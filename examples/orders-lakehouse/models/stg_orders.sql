-- kind: table
-- key: order_id
-- Raw order events land here (an upstream input, not a model).
-- Reble pins inputs like this with an Iceberg tag at run time, so the
-- branch is deterministic even while new events arrive on main.
select * from raw_events where amount > 0
