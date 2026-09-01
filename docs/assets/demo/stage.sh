#!/bin/zsh
# Stage the README hero-scenario warehouse at $1:
#   raw.orders (1,204,331 rows / 730 days, 36,121 cancelled on 214 days)
#   raw.customers, core.stg_orders, core.stg_customers,
#   core.fct_revenue_daily, core.mart_exec_dashboard
# Leaves the project ON MAIN with baseline built and the one-line
# cancelled-orders fix already applied to stg_orders (edit-first flow).
set -e
TARGET=$1
rm -rf "$TARGET"
reble init "$TARGET" >/dev/null
cd "$TARGET"
rm -rf models/demo
mkdir -p models/core seeds

cat > models/core/stg_orders.sql <<'SQL'
SELECT order_id, customer_id, date_id, status, amount
FROM raw.orders
SQL

cat > models/core/stg_customers.sql <<'SQL'
SELECT customer_id, region, signed_up
FROM raw.customers
SQL

cat > models/core/fct_revenue_daily.sql <<'SQL'
SELECT date_id,
       COUNT(*)                    AS orders,
       ROUND(SUM(amount), 2)       AS revenue
FROM core.stg_orders
GROUP BY date_id
SQL

cat > models/core/mart_exec_dashboard.sql <<'SQL'
SELECT date_trunc('month', date_id)   AS month,
       SUM(orders)                    AS orders,
       ROUND(SUM(revenue), 2)         AS revenue
FROM core.fct_revenue_daily
GROUP BY 1
SQL

python3 - <<'PY'
import csv, random, datetime
random.seed(42)
days = [datetime.date(2024, 9, 1) + datetime.timedelta(d) for d in range(730)]
cancel_days = set(random.sample(range(730), 214))

TOTAL, CANCELLED = 1_204_331, 36_121
ok_total = TOTAL - CANCELLED

# spread non-cancelled orders over all 730 days, cancelled over the 214
ok_per = [ok_total // 730] * 730
for i in random.sample(range(730), ok_total % 730):
    ok_per[i] += 1
cd = sorted(cancel_days)
c_per = {d: CANCELLED // 214 for d in cd}
for d in random.sample(cd, CANCELLED % 214):
    c_per[d] += 1

oid = 0
with open("seeds/orders.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["order_id", "customer_id", "date_id", "status", "amount"])
    for i, day in enumerate(days):
        n_ok = ok_per[i]
        n_c = c_per.get(i, 0)
        statuses = ["completed"] * n_ok + ["cancelled"] * n_c
        for s in statuses:
            oid += 1
            w.writerow([oid, random.randint(1, 42000), day.isoformat(), s,
                        round(random.uniform(4, 420), 2)])

random.seed(7)
with open("seeds/customers.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["customer_id", "region", "signed_up"])
    for c in range(1, 42001):
        w.writerow([c, random.choice(["na", "emea", "apac", "latam"]),
                    (datetime.date(2023, 1, 1)
                     + datetime.timedelta(random.randint(0, 900))).isoformat()])
PY

reble load raw.orders seeds/orders.csv
reble load raw.customers seeds/customers.csv
reble run

# a git repo, so the tape can show v0.1.0's git-follow (run creates the branch)
git init -qb main
git add -A
git -c user.email=demo@reble -c user.name=demo commit -qm "baseline models"

# the one-line fix (edit-first: the tape's `git switch -c` + `reble run` will
# infer the cascade and create the data branch)
cat > models/core/stg_orders.sql <<'SQL'
SELECT order_id, customer_id, date_id, status, amount
FROM raw.orders
WHERE status != 'cancelled'
SQL
echo "staged: $TARGET"
