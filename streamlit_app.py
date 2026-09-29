import io
from datetime import datetime
import pandas as pd
import streamlit as st

from matcher.in_memory_engine import parse_ump_file, parse_order_files, InMemoryMatchingEngine

st.set_page_config(
    page_title="UMP Matcher",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed"
)

st.markdown("""
<style>
    [data-testid="stDecoration"] { display: none; }
    .block-container { padding-top: 2rem; padding-bottom: 2rem; }
    section[data-testid="stFileUploadDropzone"] { padding: 0.75rem !important; }
</style>
""", unsafe_allow_html=True)

# Header
st.markdown("## ⚡ UMP Matcher")

# Upload Section
col_up1, col_up2 = st.columns(2)
with col_up1:
    ump_file = st.file_uploader("File UMP (.xlsx)", type=["xlsx"])
with col_up2:
    order_files = st.file_uploader(
        "File Order (.xlsx)", type=["xlsx"],
        accept_multiple_files=True,
        help="Bisa pilih lebih dari 1 file order sekaligus."
    )

# Default parameter values
date_window = 60
name_score_min = 72
amount_tol_rp = 2000
amount_tol_pct = 0.003
anomali_min_nominal = 10000
max_order_combo = 5

# Pengaturan (Opsional)
with st.expander("⚙️ Pengaturan Matching", expanded=False):
    col_p1, col_p2, col_p3 = st.columns(3)
    with col_p1:
        date_window = st.slider("Maks. Selisih Hari", min_value=15, max_value=180, value=60, step=5)
        name_score_min = st.slider("Skor Minimal Nama (%)", min_value=50, max_value=95, value=72, step=1)
    with col_p2:
        amount_tol_rp = st.number_input("Toleransi Nominal (Rp)", min_value=0, max_value=50000, value=2000, step=500)
        amount_tol_pct = st.slider("Toleransi Nominal (%)", min_value=0.0, max_value=2.0, value=0.3, step=0.1) / 100.0
    with col_p3:
        anomali_min_nominal = st.number_input("Batas Anomali (Rp)", min_value=0, max_value=100000, value=10000, step=5000)
        max_order_combo = st.slider(
            "Maks. Order per 1 Pembayaran", min_value=2, max_value=6, value=5, step=1,
            help="Kalau 1 UMP dipakai bayar beberapa order sekaligus, berapa order maksimal yang dicoba digabung."
        )

cfg = {
    'DATE_WINDOW_DAYS': date_window,
    'AMOUNT_TOL_RP': amount_tol_rp,
    'AMOUNT_TOL_PCT': amount_tol_pct,
    'NAME_SCORE_MIN': name_score_min,
    'NAME_SCORE_UNIQUE_OVERRIDE': 90,
    'MAX_ORDER_COMBO': max_order_combo,
    'MAX_UMP_COMBO': 4,
    'NOMINAL_UMUM_FREQ_THRESHOLD': 15,
    'NOMINAL_UMUM_DATE_THRESHOLD': 5,
}

col_b1, col_b2, _ = st.columns([2, 1, 5])
with col_b1:
    run_btn = st.button("🚀 Proses Matching", type="primary", use_container_width=True)
with col_b2:
    reset_btn = st.button("Reset", use_container_width=True)

if reset_btn:
    st.session_state.clear()
    st.rerun()

if run_btn:
    if not ump_file:
        st.error("File UMP belum dipilih.")
    elif not order_files:
        st.error("File Order belum dipilih.")
    else:
        with st.spinner("Memproses..."):
            try:
                umps = parse_ump_file(ump_file, anomali_min_nominal=anomali_min_nominal)
                if not umps:
                    st.error("Tidak ada baris UMP yang terbaca. Cek sheet dan format kolom.")
                else:
                    orders, order_stats = parse_order_files(order_files)
                    if not orders:
                        st.error("Tidak ada order yang valid.")
                    else:
                        engine = InMemoryMatchingEngine(orders, cfg)
                        results = engine.match_all(umps)
                        st.session_state['results'] = results
                        st.session_state['total_order'] = len(orders)
                        st.session_state['order_stats'] = order_stats
            except Exception as e:
                st.error(f"Terjadi kesalahan: {e}")

# Hasil
if 'results' in st.session_state:
    st.divider()
    results = st.session_state['results']
    df_raw = pd.DataFrame(results)

    # Metrik Ringkasan
    n_total = len(df_raw)
    n_high  = len(df_raw[df_raw['confidence'] >= 75])
    n_med   = len(df_raw[(df_raw['confidence'] >= 55) & (df_raw['confidence'] < 75)])
    n_low   = len(df_raw[(df_raw['confidence'] > 0)  & (df_raw['confidence'] < 55)])
    n_none  = len(df_raw[df_raw['confidence'] == 0])

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Total UMP", n_total, f"{st.session_state.get('total_order', 0)} Order")
    col2.metric("Confidence ≥75%", n_high)
    col3.metric("Confidence 55–74%", n_med)
    col4.metric("Confidence 1–54%", n_low)
    col5.metric("Tidak Ketemu", n_none)

    st.write("")

    # Filter & Unduh
    col_f1, col_f2, col_dl = st.columns([3, 3, 2])
    with col_f1:
        conf_filter = st.selectbox(
            "Filter Confidence",
            ["Semua", "≥75%", "55–74%", "1–54%", "0%", "Bayar >1 Order"]
        )
    with col_f2:
        search_kw = st.text_input("Cari", placeholder="Nama, no order, bukti...")

    df_filtered = df_raw.copy()
    if conf_filter == "≥75%":
        df_filtered = df_filtered[df_filtered['confidence'] >= 75]
    elif conf_filter == "55–74%":
        df_filtered = df_filtered[(df_filtered['confidence'] >= 55) & (df_filtered['confidence'] < 75)]
    elif conf_filter == "1–54%":
        df_filtered = df_filtered[(df_filtered['confidence'] > 0) & (df_filtered['confidence'] < 55)]
    elif conf_filter == "0%":
        df_filtered = df_filtered[df_filtered['confidence'] == 0]
    elif conf_filter == "Bayar >1 Order":
        df_filtered = df_filtered[df_filtered['jumlah_order'] > 1]

    if search_kw.strip():
        kw = search_kw.strip().lower()
        df_filtered = df_filtered[
            df_filtered['nama_ump'].astype(str).str.lower().str.contains(kw) |
            df_filtered['customer_order'].astype(str).str.lower().str.contains(kw) |
            df_filtered['order_no'].astype(str).str.lower().str.contains(kw) |
            df_filtered['no_bukti'].astype(str).str.lower().str.contains(kw)
        ]

    # Siapkan Excel
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
        df_export = df_raw.rename(columns={
            'no_urut': 'No Urut', 'nama_ump': 'Nama Penyetor (UMP)',
            'tanggal_ump': 'Tanggal UMP', 'no_bukti': 'No Bukti UMP',
            'nominal_ump': 'Nominal UMP (Rp)', 'order_no': 'Nomor Order Kandidat',
            'customer_order': 'Nama Customer Order', 'tanggal_order': 'Tanggal Order',
            'selisih_hari': 'Selisih Hari', 'nilai_cocok': 'Nilai Cocok (Rp)',
            'confidence': 'Confidence (%)', 'sumber': 'Sumber Kecocokan',
            'tipe': 'Tipe Kecocokan', 'bentuk_oc': 'Bentuk / No OC Kontrak',
            'kandidat_lain': 'Kandidat Alternatif', 'catatan': 'Catatan',
            'jumlah_order': 'Jumlah Order', 'rincian_order': 'Rincian Order (Bayar Banyak Order)',
        }).drop(columns=['rincian_items', 'is_anomali'], errors='ignore')
        df_export.to_excel(writer, index=False, sheet_name='Hasil Matching UMP')

        # Sheet rincian: 1 baris per order untuk UMP yang membayar banyak order
        rows_rincian = []
        for r in results:
            for ono, val in r.get('rincian_items') or []:
                rows_rincian.append({
                    'No Urut UMP': r['no_urut'], 'Nama Penyetor (UMP)': r['nama_ump'],
                    'No Bukti UMP': r['no_bukti'], 'Nominal UMP (Rp)': r['nominal_ump'],
                    'Nomor Order': ono, 'Nilai Order yang Dibayar (Rp)': val,
                    'Confidence (%)': r['confidence'],
                })
        if rows_rincian:
            pd.DataFrame(rows_rincian).to_excel(writer, index=False, sheet_name='Rincian Bayar Banyak Order')

    with col_dl:
        st.write("")
        st.download_button(
            label="📥 Unduh Excel",
            data=buffer.getvalue(),
            file_name=f"hasil_matching_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )

    # Tabel
    st.caption(f"{len(df_filtered)} dari {n_total} transaksi")
    df_view = df_filtered[[
        'no_urut', 'nama_ump', 'tanggal_ump', 'no_bukti', 'nominal_ump',
        'confidence', 'order_no', 'jumlah_order', 'customer_order', 'sumber'
    ]].copy()
    df_view['nominal_ump']  = df_view['nominal_ump'].apply(lambda v: f"Rp{v:,.0f}" if pd.notnull(v) else "-")
    df_view['confidence']   = df_view['confidence'].apply(lambda c: f"{c}%")
    df_view['tanggal_ump']  = df_view['tanggal_ump'].apply(lambda d: d.strftime('%d-%m-%Y') if pd.notnull(d) else "-")
    df_view = df_view.rename(columns={
        'no_urut': 'No', 'nama_ump': 'Nama Penyetor', 'tanggal_ump': 'Tanggal',
        'no_bukti': 'No Bukti', 'nominal_ump': 'Nominal (Rp)',
        'confidence': 'Confidence', 'order_no': 'Order Kandidat', 'jumlah_order': 'Jml Order',
        'customer_order': 'Customer', 'sumber': 'Metode'
    })
    st.dataframe(df_view, use_container_width=True, hide_index=True, height=430)

    # Detail Baris
    st.divider()
    st.markdown("**Detail Transaksi**")
    selected_idx = st.selectbox(
        "Pilih baris:",
        options=df_filtered.index,
        format_func=lambda idx: (
            f"#{df_filtered.loc[idx, 'no_urut']} · {df_filtered.loc[idx, 'nama_ump']}"
            f" · Rp{df_filtered.loc[idx, 'nominal_ump']:,.0f}"
            f" → {df_filtered.loc[idx, 'order_no'] or '—'}"
        ),
        label_visibility="collapsed"
    )

    if selected_idx is not None:
        item = df_filtered.loc[selected_idx]
        col_det1, col_det2 = st.columns(2)

        with col_det1:
            st.markdown("**Data UMP**")
            st.write(f"No Urut: **#{item['no_urut']}**")
            st.write(f"Nama: **{item['nama_ump']}**")
            tgl_ump = item['tanggal_ump'].strftime('%d-%m-%Y') if item['tanggal_ump'] else '-'
            st.write(f"Tanggal: {tgl_ump}")
            st.write(f"Nominal: **Rp{item['nominal_ump']:,.0f}**")
            st.write(f"No Bukti: `{item['no_bukti'] or '-'}`")
            if item['is_anomali']:
                st.warning("Nominal ditandai anomali (terlalu kecil).")

        with col_det2:
            st.markdown("**Hasil Pencocokan**")
            st.write(f"Confidence: **{item['confidence']}%**")
            st.write(f"Nomor Order: `{item['order_no'] or '—'}`")
            st.write(f"Customer: {item['customer_order'] or '—'}")
            tgl_ord = item['tanggal_order'].strftime('%d-%m-%Y') if item['tanggal_order'] else '-'
            st.write(f"Tanggal Order: {tgl_ord}")
            selisih = item['selisih_hari'] if pd.notnull(item['selisih_hari']) else '-'
            st.write(f"Selisih: {selisih} hari")
            st.write(f"Metode: {item['sumber'] or '—'}")
            if item['jumlah_order'] > 1:
                st.info(f"1 UMP ini dipakai bayar **{item['jumlah_order']} order** sekaligus")
                st.dataframe(
                    pd.DataFrame(item['rincian_items'], columns=['Nomor Order', 'Nilai (Rp)']),
                    hide_index=True, use_container_width=True,
                    column_config={'Nilai (Rp)': st.column_config.NumberColumn(format="Rp%d")},
                )
            if item['bentuk_oc']:
                st.write(f"Perikatan/OC: {item['bentuk_oc']}")
            if item['catatan']:
                st.caption(f"📌 {item['catatan']}")
            if item['kandidat_lain']:
                st.caption(f"Alternatif lain: `{item['kandidat_lain']}`")
