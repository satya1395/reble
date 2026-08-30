MODEL (
  name demo.orders,
  kind FULL
);

SELECT 1 AS id, 'usha'  AS customer, 10.0 AS amount
UNION ALL
SELECT 2 AS id, 'satish' AS customer, 20.0 AS amount
UNION ALL
SELECT 3 AS id, 'usha'  AS customer, 5.5 AS amount
