-- One tenant/key per script invocation. All validation precedes mutations.

-- ARGV: capacity, refill tokens/sec, cost, mode (consume or inspect).
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local mode = ARGV[4]
if not capacity or not rate or not cost
    or capacity < 1 or rate <= 0
    or cost < 1 or cost > capacity
    or (mode ~= 'consume' and mode ~= 'inspect') then
    return redis.error_reply('invalid policy or request')
end

-- Redis supplies one time source for every API replica. Convert it to milliseconds.
local now_parts = redis.call('TIME')
local now = tonumber(now_parts[1]) * 1000 + tonumber(now_parts[2]) / 1000
local state = redis.call('HMGET', KEYS[1], 'tokens', 'updated_ms', 'capacity', 'rate')


-- Missing state represents a full bucket; expiry below preserves that equivalence.
local tokens = capacity
local updated = now
if state[1] then
    if tonumber(state[3]) ~= capacity or tonumber(state[4]) ~= rate then
        return redis.error_reply('policy mismatch')
    end
    tokens = tonumber(state[1])
    updated = tonumber(state[2])
    if not tokens or not updated or tokens < 0 or tokens > capacity then
        return redis.error_reply('invalid bucket state')
    end
end

-- A backwards wall-clock jump must not cause a second refill of the same interval.
local effective_now = math.max(now, updated)
tokens = math.min(capacity, tokens + (effective_now - updated) * rate / 1000)


-- Inspection uses the same refill calculation but never subtracts or writes tokens.
local allowed = tokens >= cost
local retry_ms = 0
if not allowed then
    retry_ms = math.ceil((cost - tokens) * 1000 / rate + effective_now - now)
elseif mode == 'consume' then
    tokens = tokens - cost
end


-- Expiry and retry estimates must include any clock catch-up delay.
local full_ms = math.ceil((capacity - tokens) * 1000 / rate + effective_now - now)

-- A denied evaluation may still save refill progress, but does not spend tokens.
if mode == 'consume' then
    redis.call(
        'HSET', KEYS[1],
        'tokens', string.format('%.17g', tokens),
        'updated_ms', string.format('%.17g', effective_now),
        'capacity', ARGV[1],
        'rate', ARGV[2]
    )
    -- Expiration is safe only when an absent bucket is equivalent to a full one.
    redis.call('PEXPIRE', KEYS[1], math.max(1, full_ms))
end

-- Fractional tokens stay in the stored hash. The API receives whole remaining

-- tokens and integer millisecond delays; it does not need the fractional balance.
return {allowed and 1 or 0, math.floor(tokens), retry_ms, full_ms}
