-- RTACE failed-login window maintenance for credential stuffing, both scopes
-- in one atomic call.
--
-- KEYS[1] user window   auth:fail:user:{user_id}:1m
-- KEYS[2] ip window     auth:fail:ip:{ip}:1m
-- ARGV[1] event_id   ARGV[2] now_ts   ARGV[3] cutoff   ARGV[4] key_ttl_s
--
-- Returns { count_user, count_ip } after trimming and adding this attempt.

local now = tonumber(ARGV[2])
local counts = {}
for i = 1, 2 do
  redis.call('ZREMRANGEBYSCORE', KEYS[i], '-inf', ARGV[3])
  redis.call('ZADD', KEYS[i], now, ARGV[1])
  counts[i] = redis.call('ZCARD', KEYS[i])
  redis.call('EXPIRE', KEYS[i], tonumber(ARGV[4]))
end
return counts
