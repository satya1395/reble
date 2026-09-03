-- kind: table
-- key: order_id
-- Downstream of stg_orders: editing stg_orders automatically pulls this
-- model into the branch's scope (the downstream closure).
select
    order_id,
    user_id,
    amount,
    round(amount * 0.0825, 2)            as tax_amount,
    round(amount + amount * 0.0825, 2)   as total_with_tax,
    date_trunc('day', event_ts)          as order_date
from stg_orders
