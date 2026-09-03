-- kind: table
-- key: order_id
-- Upstream input (not a model): raw_events, where ingestion lands events
-- as they arrive — duplicates and all. Reble pins inputs like this with an
-- Iceberg tag at run time, so the branch is deterministic even while new
-- events arrive on main.
with latest as (
    select
        *,
        row_number() over (partition by order_id order by event_ts desc) as _rn
    from raw_events
),
typed as (
    select
        cast(order_id as bigint)               as order_id,
        cast(user_id as bigint)                as user_id,
        lower(status)                          as status,
        cast(amount as decimal(12, 2))         as amount,
        cast(event_ts as timestamp)            as event_ts
    from latest
    where _rn = 1
)
select *
from typed
where status = 'paid'
  and amount > 0
