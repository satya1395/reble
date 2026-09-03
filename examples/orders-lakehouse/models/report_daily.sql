-- kind: view
-- Views materialize like tables in v0 (see DECISIONS.md §3).
select
    order_date,
    count(*)             as orders,
    sum(total_with_tax)  as gross_revenue
from mart_orders
group by order_date
