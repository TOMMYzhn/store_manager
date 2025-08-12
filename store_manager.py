# store_manager.py
import streamlit as st
import pandas as pd
import os
from datetime import datetime
import json
import uuid

# 可选的模糊匹配与摄像头扫码依赖
try:
    from rapidfuzz import fuzz, process
    RAPIDFUZZ_AVAILABLE = True
except Exception:
    RAPIDFUZZ_AVAILABLE = False

try:
    import cv2
    from pyzbar import pyzbar
    CV2_AVAILABLE = True
except Exception:
    CV2_AVAILABLE = False

INVENTORY_FILE = "inventory.xlsx"
# prefered sheet name (we will detect existing sheet if different)
PRODUCT_SHEET_PREFERRED = "products"
TRANSACTION_SHEET = "transactions"
STOCK_LOG_SHEET = "stock_log"

# 更新后的中文列名 → 英文列名映射
COLUMN_MAP = {
    "分类": "category",
    "序号": "id",
    "商品名称": "name",
    "条形码": "barcode",
    "1件箱套条包盒价格": "bulk_price",  # 新增字段
    "1件箱套条包盒数量": "bulk_quantity",  # 新增字段
    "1个单位进货价格": "purchase_price",  # 修改后的字段名
    "销售价": "price",
    "利润率": "profit_margin",
    "库存": "stock",
    "位置": "location",
    "供应商": "supplier",
    "图片路径": "image_path",
    "备注": "notes"
}
REVERSE_COLUMN_MAP = {v: k for k, v in COLUMN_MAP.items()}
CHINESE_HEADERS_ORDER = list(COLUMN_MAP.keys())
ENGLISH_HEADERS_ORDER = list(COLUMN_MAP.values())  # internal order

st.set_page_config(page_title="小超市收银 + 仓库管理", layout="wide")

# -------------------------
# Helper functions
# -------------------------
def ensure_inventory_file():
    """如果 inventory.xlsx 不存在，创建并写入示例数据（使用更新后的中文表头）"""
    if not os.path.exists(INVENTORY_FILE):
        sample = [
            {
                "id": str(uuid.uuid4()), "barcode": "6901234567890", "name": "矿泉水 500ml", "category": "饮料",
                "purchase_price": 1.0, "bulk_price": 20.0, "bulk_quantity": 24,  # 新增字段示例值
                "price": 2.5, "profit_margin": None, "stock": 50, "location": "货架 A1",
                "supplier": "", "image_path": "", "notes": ""
            },
            {
                "id": str(uuid.uuid4()), "barcode": "6909876543210", "name": "方便面", "category": "食品",
                "purchase_price": 1.5, "bulk_price": 30.0, "bulk_quantity": 20,  # 新增字段示例值
                "price": 4.0, "profit_margin": None, "stock": 30, "location": "货架 B2",
                "supplier": "", "image_path": "", "notes": ""
            }
        ]
        df_sample = pd.DataFrame(sample)
        # 转换为中文表头保存
        df_chinese = df_sample.rename(columns=REVERSE_COLUMN_MAP)
        # 保证列顺序为更新后的中文表头顺序
        df_chinese = df_chinese.reindex(columns=CHINESE_HEADERS_ORDER)
        df_tx = pd.DataFrame(columns=["txn_id","datetime","items","total","paid","change","operator","note"])
        df_log = pd.DataFrame(columns=["log_id","datetime","barcode","name","change","before_stock","after_stock","type","operator","note"])
        with pd.ExcelWriter(INVENTORY_FILE, engine="openpyxl") as writer:
            # 写到首选 sheet 名称（兼容之前的脚本）
            df_chinese.to_excel(writer, sheet_name=PRODUCT_SHEET_PREFERRED, index=False)
            df_tx.to_excel(writer, sheet_name=TRANSACTION_SHEET, index=False)
            df_log.to_excel(writer, sheet_name=STOCK_LOG_SHEET, index=False)

def detect_product_sheet():
    """检测 inventory.xlsx 中实际用于商品数据的 sheet 名称（优先 products，再尝试常见中文名，否则用第一个 sheet）"""
    try:
        xls = pd.ExcelFile(INVENTORY_FILE)
        # 优先顺序：首选英文名 -> 常见中文名 -> 第一个 sheet
        candidates = [PRODUCT_SHEET_PREFERRED, "商品信息", "products"]
        for c in candidates:
            if c in xls.sheet_names:
                return c
        # fallback: first sheet
        if len(xls.sheet_names) > 0:
            return xls.sheet_names[0]
    except Exception:
        pass
    return PRODUCT_SHEET_PREFERRED

def load_products():
    """读取商品表并返回英文列名的 DataFrame（健壮处理不同表头）"""
    ensure_inventory_file()
    try:
        sheet = detect_product_sheet()
        # 读取原始表（可能是中文表头，也可能已经是英文表头）
        df_raw = pd.read_excel(INVENTORY_FILE, sheet_name=sheet, dtype=str)
        # 检查是否包含中文表头（任意一个中文 key 存在则认为是中文表头）
        raw_cols = list(df_raw.columns)
        if any(c in raw_cols for c in COLUMN_MAP.keys()):
            # 把中文列名映射成英文列名
            df = df_raw.rename(columns=COLUMN_MAP)
        else:
            # 假设已经是英文列名
            df = df_raw.copy()
        # 确保常用内部列存在
        for col in ENGLISH_HEADERS_ORDER + ["expiry_date"]:
            if col not in df.columns:
                df[col] = None
        # 类型规范
        # barcode 保持字符串
        df["barcode"] = df["barcode"].fillna("").astype(str)
        # id 字符串
        df["id"] = df["id"].fillna("").astype(str)
        # stock 填 0 并转 int
        try:
            df["stock"] = pd.to_numeric(df["stock"], errors="coerce").fillna(0).astype(int)
        except Exception:
            df["stock"] = 0
        # price / purchase_price / bulk_price numeric
        for price_col in ("price", "purchase_price", "bulk_price"):
            if price_col in df.columns:
                df[price_col] = pd.to_numeric(df[price_col], errors="coerce")
        # bulk_quantity 填 0 并转 int
        if "bulk_quantity" in df.columns:
            try:
                df["bulk_quantity"] = pd.to_numeric(df["bulk_quantity"], errors="coerce").fillna(0).astype(int)
            except Exception:
                df["bulk_quantity"] = 0
        return df
    except Exception as e:
        # 失败则返回空 DataFrame，避免整个程序崩溃
        empty = pd.DataFrame(columns=ENGLISH_HEADERS_ORDER + ["expiry_date"])
        empty["stock"] = empty["stock"].fillna(0).astype(int)
        return empty

def save_products(df):
    """将内部英文列名 DataFrame 保存到 inventory.xlsx（写回中文表头，覆盖 products sheet）并保留交易与日志表"""
    # 将内部英文列名映射回中文表头
    df_to_save = df.copy()
    df_to_save = df_to_save.rename(columns=REVERSE_COLUMN_MAP)
    # 保证中文列顺序（如果某些列不存在会补 NaN）
    df_to_save = df_to_save.reindex(columns=CHINESE_HEADERS_ORDER)
    # 读取或创建交易与日志
    try:
        tx = pd.read_excel(INVENTORY_FILE, sheet_name=TRANSACTION_SHEET)
    except Exception:
        tx = pd.DataFrame(columns=["txn_id","datetime","items","total","paid","change","operator","note"])
    try:
        log = pd.read_excel(INVENTORY_FILE, sheet_name=STOCK_LOG_SHEET)
    except Exception:
        log = pd.DataFrame(columns=["log_id","datetime","barcode","name","change","before_stock","after_stock","type","operator","note"])
    # 写回（覆盖整个文件，这样可以保证三张表同步）
    with pd.ExcelWriter(INVENTORY_FILE, engine="openpyxl", mode="w") as writer:
        df_to_save.to_excel(writer, sheet_name=PRODUCT_SHEET_PREFERRED, index=False)
        tx.to_excel(writer, sheet_name=TRANSACTION_SHEET, index=False)
        log.to_excel(writer, sheet_name=STOCK_LOG_SHEET, index=False)

def append_transaction(tx_record):
    """添加交易记录并写回文件（同时保留商品与日志）"""
    try:
        tx = pd.read_excel(INVENTORY_FILE, sheet_name=TRANSACTION_SHEET)
    except Exception:
        tx = pd.DataFrame(columns=["txn_id","datetime","items","total","paid","change","operator","note"])
    tx = pd.concat([tx, pd.DataFrame([tx_record])], ignore_index=True)
    # 读取当前商品与日志并写回
    prod = load_products()
    try:
        log = pd.read_excel(INVENTORY_FILE, sheet_name=STOCK_LOG_SHEET)
    except Exception:
        log = pd.DataFrame(columns=["log_id","datetime","barcode","name","change","before_stock","after_stock","type","operator","note"])
    # 将商品以中文表头保存
    df_prod_save = prod.rename(columns=REVERSE_COLUMN_MAP).reindex(columns=CHINESE_HEADERS_ORDER)
    with pd.ExcelWriter(INVENTORY_FILE, engine="openpyxl", mode="w") as writer:
        df_prod_save.to_excel(writer, sheet_name=PRODUCT_SHEET_PREFERRED, index=False)
        tx.to_excel(writer, sheet_name=TRANSACTION_SHEET, index=False)
        log.to_excel(writer, sheet_name=STOCK_LOG_SHEET, index=False)

def append_stock_log(log_record):
    """添加库存变更日志并写回文件（同时保留商品与交易表）"""
    try:
        log = pd.read_excel(INVENTORY_FILE, sheet_name=STOCK_LOG_SHEET)
    except Exception:
        log = pd.DataFrame(columns=["log_id","datetime","barcode","name","change","before_stock","after_stock","type","operator","note"])
    log = pd.concat([log, pd.DataFrame([log_record])], ignore_index=True)
    prod = load_products()
    try:
        tx = pd.read_excel(INVENTORY_FILE, sheet_name=TRANSACTION_SHEET)
    except Exception:
        tx = pd.DataFrame(columns=["txn_id","datetime","items","total","paid","change","operator","note"])
    df_prod_save = prod.rename(columns=REVERSE_COLUMN_MAP).reindex(columns=CHINESE_HEADERS_ORDER)
    with pd.ExcelWriter(INVENTORY_FILE, engine="openpyxl", mode="w") as writer:
        df_prod_save.to_excel(writer, sheet_name=PRODUCT_SHEET_PREFERRED, index=False)
        tx.to_excel(writer, sheet_name=TRANSACTION_SHEET, index=False)
        log.to_excel(writer, sheet_name=STOCK_LOG_SHEET, index=False)

# -------------------------
# Search utilities
# -------------------------
def search_by_name(query, products_df, limit=5):
    query = str(query).strip()
    if len(query) == 0:
        return pd.DataFrame()
    # exact contains
    if "name" not in products_df.columns:
        return pd.DataFrame()
    subs = products_df[products_df["name"].astype(str).str.contains(query, case=False, na=False)]
    if len(subs) >= 1 or not RAPIDFUZZ_AVAILABLE:
        return subs.head(limit)
    # fuzzy with rapidfuzz
    choices = products_df["name"].astype(str).tolist()
    results = process.extract(query, choices, scorer=fuzz.WRatio, limit=limit)
    names = [r[0] for r in results]
    return products_df[products_df["name"].isin(names)]

def search_by_barcode(barcode, products_df):
    barcode = str(barcode).strip()
    if barcode == "":
        return pd.DataFrame()
    if "barcode" not in products_df.columns:
        return pd.DataFrame()
    df = products_df[products_df["barcode"].fillna("").astype(str) == barcode]
    return df

# -------------------------
# Inventory update
# -------------------------
def update_stock_by_barcode(barcode, delta, operator="system", typ="out", note=""):
    df = load_products()
    idx = df.index[df["barcode"].fillna("").astype(str) == str(barcode)]
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    if len(idx) == 0:
        return False, "未找到条码对应商品"
    i = idx[0]
    before = int(df.at[i, "stock"])
    after = before + int(delta)
    df.at[i, "stock"] = int(after)
    save_products(df)
    log = {
        "log_id": str(uuid.uuid4()),
        "datetime": now,
        "barcode": str(barcode),
        "name": df.at[i].get("name", ""),
        "change": int(delta),
        "before_stock": int(before),
        "after_stock": int(after),
        "type": typ,
        "operator": operator,
        "note": note
    }
    append_stock_log(log)
    return True, "更新成功"

# -------------------------
# Streamlit UI (保持原有逻辑，只用英文列名访问)
# -------------------------
st.title("🛒 小超市收银与仓库管理系统")

# load data
products = load_products()

# sidebar: mode select
mode = st.sidebar.selectbox("选择功能", ["收银台 (POS)", "商品查询 / 浏览", "仓库管理（入库/出库）", "商品维护（增删改）", "导出 / 备份"])
st.sidebar.markdown("说明：扫码枪可作为键盘输入，扫码后自动回车。")

# -------------------------
# POS 收银界面
# -------------------------
if mode == "收银台 (POS)":
    st.header("收银台")
    col1, col2 = st.columns([2,1])
    with col1:
        st.subheader("扫码 / 输入商品")
        barcode_input = st.text_input("输入条码或商品名称并回车（支持模糊匹配）", key="scan_input")
        qty = st.number_input("数量", min_value=1, value=1, step=1)
        add_btn = st.button("添加到购物车")
        # cart stored in session
        if "cart" not in st.session_state:
            st.session_state.cart = []
        if add_btn and barcode_input.strip() != "":
            # try barcode exact match first
            df_bar = search_by_barcode(barcode_input, products)
            if len(df_bar) > 0:
                row = df_bar.iloc[0].to_dict()
            else:
                df_name = search_by_name(barcode_input, products, limit=1)
                if len(df_name) > 0:
                    row = df_name.iloc[0].to_dict()
                else:
                    st.warning("未找到商品，请先在商品维护中新增。")
                    row = None
            if row is not None:
                item = {
                    "id": row.get("id",""),
                    "barcode": str(row.get("barcode","")),
                    "name": row.get("name",""),
                    "price": float(row.get("price",0.0) if row.get("price") is not None else 0.0),
                    "qty": int(qty),
                    "subtotal": float(row.get("price",0.0) if row.get("price") is not None else 0.0) * int(qty)
                }
                st.session_state.cart.append(item)
                st.success(f'已添加：{item["name"]} × {item["qty"]}')
                # clear input
                st.session_state["scan_input"] = ""

        st.subheader("购物车")
        if "cart" in st.session_state and len(st.session_state.cart) > 0:
            cart_df = pd.DataFrame(st.session_state.cart)
            st.dataframe(cart_df[["barcode","name","price","qty","subtotal"]])
            total = cart_df["subtotal"].sum()
            st.markdown(f"**总计：¥ {total:.2f}**")
            paid = st.number_input("实收金额 (¥)", min_value=0.0, value=float(total), step=0.1)
            if st.button("结账并打印小票 / 更新库存"):
                # perform checkout: reduce stock and record transaction
                success = True
                messages = []
                for it in st.session_state.cart:
                    ok, msg = update_stock_by_barcode(it["barcode"], -it["qty"], operator="收银", typ="sale", note="POS 结账")
                    if not ok:
                        success = False
                        messages.append(f'{it["name"]}: {msg}')
                if success:
                    txn = {
                        "txn_id": str(uuid.uuid4()),
                        "datetime": datetime.now().isoformat(sep=" ", timespec="seconds"),
                        "items": json.dumps(st.session_state.cart, ensure_ascii=False),
                        "total": float(total),
                        "paid": float(paid),
                        "change": float(paid - total),
                        "operator": "收银",
                        "note": ""
                    }
                    append_transaction(txn)
                    st.success(f"结账完成。找零：¥ {txn['change']:.2f}")
                    st.session_state.cart = []
                else:
                    st.error("部分商品结账失败：" + "; ".join(messages))
        else:
            st.info("购物车空。用上方输入框扫码或输入商品名添加。")

    with col2:
        st.subheader("快速操作")
        if st.button("清空购物车"):
            st.session_state.cart = []
            st.success("购物车已清空。")
        if st.button("载入示例商品（如果你刚刚新建了空表）"):
            ensure_inventory_file()
            st.success("已确保示例 inventory.xlsx 存在。请刷新页面查看。")
        st.markdown("**最近交易（最近 10 条）**")
        try:
            tx = pd.read_excel(INVENTORY_FILE, sheet_name=TRANSACTION_SHEET)
            if len(tx) == 0:
                st.write("暂无交易记录。")
            else:
                show = tx.tail(10).iloc[::-1]
                st.dataframe(show[["txn_id","datetime","total","paid","change"]])
        except Exception:
            st.write("暂无交易记录。")

# -------------------------
# 商品查询 / 浏览
# -------------------------
elif mode == "商品查询 / 浏览":
    st.header("商品查询 / 浏览")
    q = st.text_input("输入条码或商品名进行查询（支持模糊匹配）")
    if st.button("搜索") or q.strip() != "":
        df_bar = search_by_barcode(q, products)
        if len(df_bar) > 0:
            st.success("按条码精确匹配：")
            st.dataframe(df_bar)
        else:
            df_name = search_by_name(q, products, limit=10)
            if len(df_name) == 0:
                st.warning("未找到匹配商品。")
            else:
                st.success(f"匹配到 {len(df_name)} 条结果（基于名称）")
                st.dataframe(df_name)
    st.markdown("---")
    st.subheader("全部商品一览（可排序）")
    st.dataframe(products)

# -------------------------
# 仓库管理（入库/出库）
# -------------------------
elif mode == "仓库管理（入库/出库）":
    st.header("仓库管理（入库 / 出库）")
    action = st.radio("操作类型", ["入库", "出库", "库存调整"])
    with st.form("stock_form"):
        barcode = st.text_input("条码（barcode）")
        name = st.text_input("商品名称（如果条码不存在可使用名称匹配）")
        qty = st.number_input("数量（正数）", min_value=1, value=1, step=1)
        operator = st.text_input("操作员", value="仓库")
        note = st.text_area("备注", value="")
        submitted = st.form_submit_button("提交")
        if submitted:
            # try find by barcode
            df_bar = search_by_barcode(barcode, products) if barcode.strip() != "" else pd.DataFrame()
            if len(df_bar) == 0 and name.strip() != "":
                df_name = search_by_name(name, products, limit=1)
                if len(df_name) > 0:
                    df_bar = df_name
            if len(df_bar) == 0:
                st.error("未找到商品，请先在商品维护中新增条目（或填写正确条码/名称）。")
            else:
                b = str(df_bar.iloc[0]["barcode"])
                if action == "入库":
                    delta = int(qty)
                    typ = "in"
                elif action == "出库":
                    delta = -int(qty)
                    typ = "out"
                else:
                    delta = int(qty)  # 调整也当做正数变更（可以用 note 指明）
                    typ = "adjust"
                ok, msg = update_stock_by_barcode(b, delta, operator=operator, typ=typ, note=note)
                if ok:
                    st.success(f"{action} 成功：{df_bar.iloc[0].get('name','')} 数量变更 {delta}")
                else:
                    st.error("操作失败：" + msg)

    st.markdown("库存快照（前 200 条）")
    st.dataframe(load_products().head(200))

# -------------------------
# 商品维护（增删改）
# -------------------------
elif mode == "商品维护（增删改）":
    st.header("商品维护（新增 / 修改 / 删除）")
    st.subheader("新增商品")
    with st.form("add_form"):
        name = st.text_input("商品名称")
        barcode = st.text_input("条码")
        category = st.text_input("分类")
        price = st.number_input("售价 (¥)", min_value=0.0, value=1.0, step=0.1)
        purchase_price = st.number_input("1个单位进货价格 (¥)", min_value=0.0, value=0.0, step=0.1)
        bulk_price = st.number_input("1件箱套条包盒价格 (¥)", min_value=0.0, value=0.0, step=0.1)
        bulk_quantity = st.number_input("1件箱套条包盒数量", min_value=0, value=0, step=1)
        location = st.text_input("货位")
        stock = st.number_input("初始库存", min_value=0, value=0, step=1)
        notes = st.text_input("备注")
        add_sub = st.form_submit_button("添加商品")
        if add_sub:
            df = load_products()
            new = {
                "id": str(uuid.uuid4()),
                "barcode": str(barcode).strip(),
                "name": name.strip(),
                "category": category.strip(),
                "price": float(price),
                "purchase_price": float(purchase_price),
                "bulk_price": float(bulk_price),
                "bulk_quantity": int(bulk_quantity),
                "location": location.strip(),
                "stock": int(stock),
                "expiry_date": "",
                "notes": notes.strip(),
                "supplier": "",
                "image_path": "",
                "profit_margin": None
            }
            df = pd.concat([df, pd.DataFrame([new])], ignore_index=True)
            save_products(df)
            # log initial stock if > 0
            if int(stock) > 0 and new["barcode"] != "":
                append_stock_log({
                    "log_id": str(uuid.uuid4()),
                    "datetime": datetime.now().isoformat(sep=" ", timespec="seconds"),
                    "barcode": new["barcode"],
                    "name": new["name"],
                    "change": int(stock),
                    "before_stock": 0,
                    "after_stock": int(stock),
                    "type": "in_initial",
                    "operator": "system",
                    "note": "新建商品初始库存"
                })
            st.success("商品已添加，已保存到 inventory.xlsx")

    st.markdown("---")
    st.subheader("按条码编辑 / 删除")
    e_barcode = st.text_input("输入要编辑的条码", key="edit_bar")
    if st.button("载入商品"):
        products = load_products()  # reload to get latest
        df_e = search_by_barcode(e_barcode, products) if e_barcode.strip() != "" else pd.DataFrame()
        if len(df_e) == 0:
            st.warning("未找到该条码商品")
        else:
            r = df_e.iloc[0]
            with st.form("edit_form"):
                name2 = st.text_input("商品名称", value=r.get("name",""))
                category2 = st.text_input("分类", value=r.get("category",""))
                price2 = st.number_input("售价 (¥)", min_value=0.0, value=float(r.get("price",0.0) if r.get("price") is not None else 0.0), step=0.1)
                purchase_price2 = st.number_input("1个单位进货价格 (¥)", min_value=0.0, value=float(r.get("purchase_price",0.0) if r.get("purchase_price") is not None else 0.0), step=0.1)
                bulk_price2 = st.number_input("1件箱套条包盒价格 (¥)", min_value=0.0, value=float(r.get("bulk_price",0.0) if r.get("bulk_price") is not None else 0.0), step=0.1)
                bulk_quantity2 = st.number_input("1件箱套条包盒数量", min_value=0, value=int(r.get("bulk_quantity",0)), step=1)
                location2 = st.text_input("货位", value=r.get("location",""))
                stock2 = st.number_input("库存", min_value=0, value=int(r.get("stock",0)), step=1)
                notes2 = st.text_input("备注", value=r.get("notes",""))
                save_btn = st.form_submit_button("保存修改")
                del_btn = st.form_submit_button("删除商品")
                if save_btn:
                    df_all = load_products()
                    idx = df_all.index[df_all["barcode"].fillna("").astype(str) == str(e_barcode)]
                    if len(idx) > 0:
                        i = idx[0]
                        before_stock = int(df_all.at[i,"stock"])
                        df_all.at[i,"name"] = name2
                        df_all.at[i,"category"] = category2
                        df_all.at[i,"price"] = float(price2)
                        df_all.at[i,"purchase_price"] = float(purchase_price2)
                        df_all.at[i,"bulk_price"] = float(bulk_price2)
                        df_all.at[i,"bulk_quantity"] = int(bulk_quantity2)
                        df_all.at[i,"location"] = location2
                        df_all.at[i,"stock"] = int(stock2)
                        df_all.at[i,"notes"] = notes2
                        save_products(df_all)
                        st.success("修改已保存")
                        # if stock changed, add log
                        if before_stock != int(stock2):
                            append_stock_log({
                                "log_id": str(uuid.uuid4()),
                                "datetime": datetime.now().isoformat(sep=" ", timespec="seconds"),
                                "barcode": str(e_barcode),
                                "name": name2,
                                "change": int(stock2) - before_stock,
                                "before_stock": int(before_stock),
                                "after_stock": int(stock2),
                                "type": "adjust_manual",
                                "operator": "库存编辑",
                                "note": "人工修改库存"
                            })
                    else:
                        st.error("未能定位要修改的商品")
                if del_btn:
                    df_all = load_products()
                    df_all = df_all[df_all["barcode"].fillna("").astype(str) != str(e_barcode)]
                    save_products(df_all)
                    st.success("商品已删除（若存在）")

# -------------------------
# 导出 / 备份
# -------------------------
elif mode == "导出 / 备份":
    st.header("导出 / 备份")
    st.markdown("当前 inventory 文件位置：{}".format(os.path.abspath(INVENTORY_FILE)))
    if st.button("下载 inventory.xlsx"):
        with open(INVENTORY_FILE, "rb") as f:
            data = f.read()
        st.download_button("点击下载 Excel 文件", data, file_name="inventory.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    st.markdown("**库存日志 / 交易日志 快照**")
    try:
        tx = pd.read_excel(INVENTORY_FILE, sheet_name=TRANSACTION_SHEET)
        log = pd.read_excel(INVENTORY_FILE, sheet_name=STOCK_LOG_SHEET)
        st.write("交易记录 (最近 50):")
        st.dataframe(tx.tail(50).iloc[::-1])
        st.write("库存变更日志 (最近 50):")
        st.dataframe(log.tail(50).iloc[::-1])
    except Exception as e:
        st.write("暂无记录或读取失败：", e)

# -------------------------
# end
# -------------------------
st.markdown("---")
st.caption("说明：此系统为轻量级示例，实现库存与收银的基本流程。可根据需求定制导出小票打印、条码批量导入、会员折扣、报表统计（滞销/畅销）等功能。")