-- kind: table
-- key: order_id
-- Downstream of stg_orders: editing stg_orders automatically pulls this
-- model into the branch's scope (the downstream closure).
select
    order_id,
    amount,
    amount * 2 as amount_doubled
from stg_orders
