import pandas as pd
import json
import os

# 读取所有月份数据
data_dir = "sales_report/out/clean"
months = ["2025-10", "2025-11", "2025-12", "2026-01", "2026-02", "2026-03", 
          "2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"]

all_data = []
for month in months:
    file_path = os.path.join(data_dir, f"{month}.csv")
    df = pd.read_csv(file_path)
    df['month'] = month
    all_data.append(df)

df_all = pd.concat(all_data, ignore_index=True)

# 分离正常订单和退货订单
df_normal = df_all[df_all['is_return'] == 'No'].copy()
df_return = df_all[df_all['is_return'] == 'Yes'].copy()

# 1. 整体概况
total_orders = df_normal['order_id'].nunique()
total_revenue = round(df_normal['amount'].sum(), 2)
total_return_orders = len(df_return)
# 退货金额取绝对值（原始数据是负数）
total_return_amount = round(abs(df_return['amount'].sum()), 2)
# 净收入 = 正常收入 + 退货金额（负数相加）
net_revenue = round(total_revenue + df_return['amount'].sum(), 2)

overview = {
    "total_orders": total_orders,
    "total_revenue": total_revenue,
    "total_return_orders": total_return_orders,
    "total_return_amount": total_return_amount,
    "net_revenue": net_revenue
}

# 2. 月度趋势
monthly = []
for month in months:
    month_normal = df_normal[df_normal['month'] == month]
    month_return = df_return[df_return['month'] == month]
    
    month_revenue = month_normal['amount'].sum()
    month_return_abs = abs(month_return['amount'].sum())
    
    monthly.append({
        "month": month,
        "orders": int(month_normal['order_id'].nunique()),
        "revenue": round(month_revenue, 2),
        "return_orders": int(len(month_return)),
        "return_amount": round(month_return_abs, 2),
        "net_revenue": round(month_revenue + month_return['amount'].sum(), 2)
    })

# 3. 月度环比
monthly_growth = []
for i in range(1, len(months)):
    prev = monthly[i-1]
    curr = monthly[i]
    
    orders_growth = round((curr['orders'] - prev['orders']) / prev['orders'] * 100, 2) if prev['orders'] > 0 else 0
    revenue_growth = round((curr['revenue'] - prev['revenue']) / prev['revenue'] * 100, 2) if prev['revenue'] > 0 else 0
    
    monthly_growth.append({
        "month": curr['month'],
        "orders_growth": orders_growth,
        "revenue_growth": revenue_growth
    })

# 4. 品类分析
category_data = df_normal.groupby('category').agg(
    orders=('order_id', 'nunique'),
    revenue=('amount', 'sum'),
    total_qty=('qty', 'sum')
).reset_index()
category_data['avg_unit_price'] = round(category_data['revenue'] / category_data['total_qty'], 2)
category_data['pct_of_total'] = round(category_data['revenue'] / total_revenue * 100, 2)

category_breakdown = []
for _, row in category_data.iterrows():
    category_breakdown.append({
        "category": row['category'],
        "orders": int(row['orders']),
        "revenue": round(row['revenue'], 2),
        "avg_unit_price": row['avg_unit_price'],
        "pct_of_total": row['pct_of_total']
    })

# 5. 门店分析
store_data = df_normal.groupby('store').agg(
    orders=('order_id', 'nunique'),
    revenue=('amount', 'sum')
).reset_index()
store_data['avg_order_value'] = round(store_data['revenue'] / store_data['orders'], 2)
store_data['pct_of_total'] = round(store_data['revenue'] / total_revenue * 100, 2)

store_breakdown = []
for _, row in store_data.iterrows():
    store_breakdown.append({
        "store": row['store'],
        "orders": int(row['orders']),
        "revenue": round(row['revenue'], 2),
        "avg_order_value": row['avg_order_value'],
        "pct_of_total": row['pct_of_total']
    })

# 6. 客户TOP10
customer_data = df_normal.groupby('customer').agg(
    total_orders=('order_id', 'nunique'),
    total_spending=('amount', 'sum')
).reset_index().sort_values('total_spending', ascending=False).head(10)

top_customers = []
for _, row in customer_data.iterrows():
    top_customers.append({
        "customer": row['customer'],
        "total_orders": int(row['total_orders']),
        "total_spending": round(row['total_spending'], 2)
    })

# 7. 退货分析
return_rate = round(total_return_orders / total_orders * 100, 2)
avg_monthly_returns = round(total_return_orders / len(months), 2)
return_amount_pct = round(total_return_amount / total_revenue * 100, 2)

# 退货高发月
return_by_month = df_return.groupby('month').size()
peak_return_month = return_by_month.idxmax() if len(return_by_month) > 0 else None

return_analysis = {
    "return_rate": return_rate,
    "avg_monthly_returns": avg_monthly_returns,
    "return_amount_pct": return_amount_pct,
    "peak_return_month": peak_return_month
}

# 8. 全年同比 (2025-10 ~ 2025-12 vs 2026-01 ~ 2026-09)
data_2025_q4 = df_normal[df_normal['month'].isin(["2025-10", "2025-11", "2025-12"])]
data_2026 = df_normal[df_normal['month'].isin(["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"])]

if len(data_2025_q4) > 0 and len(data_2026) > 0:
    yoy_comparison = {
        "2025_q4_orders": int(data_2025_q4['order_id'].nunique()),
        "2025_q4_revenue": round(data_2025_q4['amount'].sum(), 2),
        "2026_orders": int(data_2026['order_id'].nunique()),
        "2026_revenue": round(data_2026['amount'].sum(), 2),
        "orders_change_pct": round((data_2026['order_id'].nunique() - data_2025_q4['order_id'].nunique()) / data_2025_q4['order_id'].nunique() * 100, 2),
        "revenue_change_pct": round((data_2026['amount'].sum() - data_2025_q4['amount'].sum()) / data_2025_q4['amount'].sum() * 100, 2)
    }
else:
    yoy_comparison = {"note": "数据不足，无法计算同比"}

# 组装最终结果
result = {
    "overview": overview,
    "monthly": monthly,
    "monthly_growth": monthly_growth,
    "category_breakdown": category_breakdown,
    "store_breakdown": store_breakdown,
    "top_customers": top_customers,
    "return_analysis": return_analysis,
    "yoy_comparison": yoy_comparison
}

# 输出到JSON
output_path = "sales_report/out/summary.json"
with open(output_path, 'w', encoding='utf-8') as f:
    json.dump(result, f, ensure_ascii=False, indent=2)

print("Statistical analysis completed! Results saved to sales_report/out/summary.json")
print(f"\n=== Overview ===")
print(f"Total Orders: {total_orders}")
print(f"Total Revenue: {total_revenue}")
print(f"Return Orders: {total_return_orders}")
print(f"Return Amount: {total_return_amount}")
print(f"Net Revenue: {net_revenue}")
