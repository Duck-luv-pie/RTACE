-- RTACE per-transaction check. One atomic call replaces four round trips:
-- replay SET NX, burst window maintenance, session read, session write.
-- Shared by the Python detection engine and the Go decision service, so the
-- detection semantics live in exactly one place.
--
-- KEYS[1] replay key        replay:seen:{hash}
-- KEYS[2] burst key         burst:{user_id}:1m
-- KEYS[3] session key       session:{user_id}   (hash: lat, lon, ts)
--
-- ARGV[1]  record_ref        identity of THIS delivery ("topic:partition:offset"
--                            from Kafka, or "decision:{event_id}" from the
--                            synchronous decision service)
-- ARGV[2]  replay_ttl_s
-- ARGV[3]  event_id
-- ARGV[4]  now_ts            event time, unix seconds (float)
-- ARGV[5]  burst_cutoff      now_ts - burst window
-- ARGV[6]  burst_key_ttl_s
-- ARGV[7]  latitude
-- ARGV[8]  longitude
-- ARGV[9]  session_ttl_s
-- ARGV[10] max_velocity_kmh
-- ARGV[11] min_time_s        gaps shorter than this are scored as this long
--
-- Returns { replay_status, stored_ref, burst_count, geo_status,
--           distance_km, elapsed_s, velocity_kmh, prev_lat, prev_lon }
--   replay_status: first | redelivery | prescored | replay
--   geo_status:    baseline | ok | anomaly | out_of_order | skipped
-- Floats are returned as strings: Redis truncates Lua numbers to integers.

local function fmt(x) return string.format('%.3f', x) end

local function haversine_km(lat1, lon1, lat2, lon2)
  local rad = math.pi / 180
  local dphi = (lat2 - lat1) * rad
  local dl = (lon2 - lon1) * rad
  local a = math.sin(dphi / 2) ^ 2
          + math.cos(lat1 * rad) * math.cos(lat2 * rad) * math.sin(dl / 2) ^ 2
  if a > 1 then a = 1 end
  return 2 * 6371.0 * math.asin(math.sqrt(a))
end

local record_ref = ARGV[1]
local event_id = ARGV[3]

-- 1. Replay --------------------------------------------------------------
local set_ok = redis.call('SET', KEYS[1], record_ref, 'NX', 'EX', tonumber(ARGV[2]))
local replay_status = 'first'
local stored = record_ref
if not set_ok then
  stored = redis.call('GET', KEYS[1]) or ''
  if stored == record_ref then
    replay_status = 'redelivery'          -- same delivery again: not a replay
  elseif stored == 'decision:' .. event_id then
    replay_status = 'prescored'           -- decision service already scored it
  else
    replay_status = 'replay'
  end
end

if replay_status == 'prescored' then
  -- The decision service already advanced burst and session state for this
  -- event; touching it again would double count.
  return { replay_status, stored, 0, 'skipped', '0', '0', '0', '', '' }
end

-- 2. Burst window ---------------------------------------------------------
local now = tonumber(ARGV[4])
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', ARGV[5])
redis.call('ZADD', KEYS[2], now, event_id)
local burst_count = redis.call('ZCARD', KEYS[2])
redis.call('EXPIRE', KEYS[2], tonumber(ARGV[6]))

-- 3. Geo velocity against the trusted baseline --------------------------
local lat, lon = tonumber(ARGV[7]), tonumber(ARGV[8])
local s = redis.call('HMGET', KEYS[3], 'lat', 'lon', 'ts')
local geo_status, dist, elapsed, vel = 'baseline', 0, 0, 0
local prev_lat, prev_lon = '', ''
if s[1] and s[2] and s[3] then
  prev_lat, prev_lon = s[1], s[2]
  elapsed = now - tonumber(s[3])
  if elapsed < 0 then
    geo_status = 'out_of_order'
  else
    dist = haversine_km(tonumber(s[1]), tonumber(s[2]), lat, lon)
    local hours = math.max(elapsed, tonumber(ARGV[11])) / 3600
    vel = dist / hours
    if vel > tonumber(ARGV[10]) then geo_status = 'anomaly' else geo_status = 'ok' end
  end
end
-- Only clean transactions advance the baseline: an anomalous location must
-- not become trusted, and a late event must not rewind it.
if geo_status == 'baseline' or geo_status == 'ok' then
  redis.call('HSET', KEYS[3], 'lat', ARGV[7], 'lon', ARGV[8], 'ts', ARGV[4])
  redis.call('EXPIRE', KEYS[3], tonumber(ARGV[9]))
end

return { replay_status, stored, burst_count, geo_status,
         fmt(dist), fmt(elapsed), fmt(vel), prev_lat, prev_lon }
