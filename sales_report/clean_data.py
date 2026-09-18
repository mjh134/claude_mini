import os
import re
from datetime import datetime
import csv

def normalize_date(date_str):
    """将各种日期格式统一成 YYYY-MM-DD"""
    date_str = date_str.strip()
    # 尝试多种分隔符
    for sep in ['-', '/', '.']:
        if sep in date_str:
            parts = date_str.split(sep)
            if len(parts) == 3:
                year, month, day = parts
                year = year.zfill(4)
                month = month.zfill(2)
                day = day.zfill(2)
                return f"{year}-{month}-{day}"
    return date_str

def normalize_category(cat):
    """统一 category 为首字母大写"""
    cat = cat.strip()
    cat_lower = cat.lower()
    if cat_lower == 'milk tea':
        return 'Milk Tea'
    elif cat_lower == 'coffee':
        return 'Coffee'
    elif cat_lower == 'bakery':
        return 'Bakery'
    return cat.strip()

def normalize_store(store):
    """去掉首尾空格"""
    return store.strip()

def normalize_customer(customer):
    """去掉首尾空格"""
    return customer.strip()

def clean_amount(amount_str):
    """清洗金额，保留两位小数"""
    if amount_str is None or amount_str == '':
        return None
    try:
        return round(float(amount_str), 2)
    except:
        return None

def process_file(filepath):
    """处理单个文件，返回统计数据"""
    stats = {
        'total_rows': 0,
        'duplicate_rows': 0,
        'empty_qty_rows': 0,
        'filled_amount_rows': 0,
        'return_rows': 0
    }
    
    seen_order_ids = set()
    cleaned_rows = []
    
    with open(filepath, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames
        
        for row in reader:
            stats['total_rows'] += 1
            order_id = row['order_id'].strip()
            
            # 检查 order_id 是否重复
            if order_id in seen_order_ids:
                stats['duplicate_rows'] += 1
                continue
            seen_order_ids.add(order_id)
            
            # 清洗日期
            date = normalize_date(row['date'])
            
            # 清洗 store, customer
            store = normalize_store(row['store'])
            customer = normalize_customer(row['customer'])
            
            # 清洗 category
            category = normalize_category(row['category'])
            
            # 处理 qty
            qty_str = row['qty'].strip() if row['qty'] else ''
            if qty_str == '':
                # qty 为空，整行剔除
                stats['empty_qty_rows'] += 1
                continue
            
            try:
                qty = int(qty_str)
            except:
                stats['empty_qty_rows'] += 1
                continue
            
            # 处理 unit_price
            try:
                unit_price = float(row['unit_price'].strip())
            except:
                unit_price = 0.0
            
            # 处理 amount
            amount_str = row['amount'].strip() if row['amount'] else ''
            if amount_str == '' or amount_str is None:
                # amount 为空，用 qty × unit_price 补算
                amount = round(qty * unit_price, 2)
                stats['filled_amount_rows'] += 1
            else:
                amount = round(float(amount_str), 2)
            
            # 检查是否是退货单
            is_return = 'Yes' if qty < 0 else 'No'
            if qty < 0:
                stats['return_rows'] += 1
            
            cleaned_rows.append({
                'order_id': order_id,
                'date': date,
                'store': store,
                'category': category,
                'customer': customer,
                'qty': qty,
                'unit_price': round(unit_price, 2),
                'amount': amount,
                'is_return': is_return
            })
    
    return stats, cleaned_rows

def main():
    raw_dir = 'sales_report/raw'
    out_dir = 'sales_report/out/clean'
    
    all_stats = {
        'total_rows': 0,
        'duplicate_rows': 0,
        'empty_qty_rows': 0,
        'filled_amount_rows': 0,
        'return_rows': 0
    }
    
    # 获取所有月份文件
    files = sorted([f for f in os.listdir(raw_dir) if f.endswith('.csv')])
    
    for filename in files:
        filepath = os.path.join(raw_dir, filename)
        stats, cleaned_rows = process_file(filepath)
        
        # 累加统计
        for key in all_stats:
            all_stats[key] += stats[key]
        
        # 输出清洗后的文件
        out_path = os.path.join(out_dir, filename)
        with open(out_path, 'w', encoding='utf-8', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=[
                'order_id', 'date', 'store', 'category', 'customer',
                'qty', 'unit_price', 'amount', 'is_return'
            ])
            writer.writeheader()
            writer.writerows(cleaned_rows)
        
        print(f"已处理: {filename} -> {len(cleaned_rows)} 行 (剔除空qty: {stats['empty_qty_rows']}, 去重: {stats['duplicate_rows']}, 补算amount: {stats['filled_amount_rows']}, 退货: {stats['return_rows']})")
    
    # 输出总报告
    print("\n" + "="*60)
    print("数据清洗统计报告")
    print("="*60)
    print(f"总共处理原始数据行数: {all_stats['total_rows']}")
    print(f"去掉重复行数: {all_stats['duplicate_rows']}")
    print(f"剔除空qty的废行: {all_stats['empty_qty_rows']}")
    print(f"补算amount为空的行: {all_stats['filled_amount_rows']}")
    print(f"退货单数量(qty<0): {all_stats['return_rows']}")
    print("="*60)
    
    # 写入报告文件
    report_path = os.path.join(out_dir, 'cleaning_report.txt')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write("数据清洗统计报告\n")
        f.write("="*60 + "\n")
        f.write(f"总共处理原始数据行数: {all_stats['total_rows']}\n")
        f.write(f"去掉重复行数: {all_stats['duplicate_rows']}\n")
        f.write(f"剔除空qty的废行: {all_stats['empty_qty_rows']}\n")
        f.write(f"补算amount为空的行: {all_stats['filled_amount_rows']}\n")
        f.write(f"退货单数量(qty<0): {all_stats['return_rows']}\n")
        f.write("="*60 + "\n")
    
    print(f"\n报告已保存到: {report_path}")

if __name__ == '__main__':
    main()
