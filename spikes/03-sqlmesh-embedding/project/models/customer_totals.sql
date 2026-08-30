MODEL (
  name demo.customer_totals,
  kind FULL
);

SELECT
  customer,
  COUNT(*)    AS order_count,
  SUM(amount) AS total_amount
FROM demo.orders
GROUP BY customer
