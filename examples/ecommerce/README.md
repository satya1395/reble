# E-Commerce Analytics Example

A complete example showing how to use Reble for an e-commerce analytics pipeline.

## Data Model

```
Raw Data (PostgreSQL)
  ├─ orders
  ├─ users
  ├─ products
  └─ order_items

Transformed (Iceberg)
  ├─ stg_orders
  ├─ stg_users
  ├─ stg_products
  └─ fact_orders (column-level lineage)
```

## Running This Example

1. **Start Reble**
   ```bash
   cd reble
   docker-compose up
   ```

2. **Initialize sample data**
   ```bash
   # TBD - seed data script
   ```

3. **Create a branch for feature work**
   ```bash
   reble branch create add-customer-metrics
   ```

4. **Run transformations**
   ```bash
   reble run
   ```

5. **Check lineage**
   ```bash
   reble lineage show fact_orders
   ```

   Output shows:
   ```
   fact_orders <- stg_orders, stg_products
              <- orders, users, products (raw)
   
   Column tracking:
   - fact_orders.customer_name -> users.name
   - fact_orders.total_amount -> orders.total
   ```

6. **Modify a transformation**
   Edit `models/fact_orders.sql` to add a new column

7. **Check impact**
   ```bash
   reble impact show
   ```

8. **Merge to main**
   ```bash
   reble branch merge add-customer-metrics
   ```

## Files

- `models/` — SQLMesh transformation definitions
- `seeds/` — Raw data for testing
- `tests/` — Data validation tests
- `macros/` — Reusable SQL functions

## Next Steps

- Try modifying `models/fact_orders.sql`
- Add a new transformation in `models/`
- Check how column lineage updates automatically
