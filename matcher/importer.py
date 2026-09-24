"""Baca file Excel order & UMP, simpan ke database."""
import openpyxl
from models import db, Order, OrderLine, UMPRecord, UploadBatch
from matcher.norm_utils import norm_name


REQUIRED_ORDER_COLUMNS = [
    'Nomor Order', 'Nama Customer', 'Order Created Date', 'Total Fee',
    'PPN Amount', 'No Invoice', 'Line Order Status', 'Line Number',
    'Bentuk Perikatan', 'Nomor OC/Kontrak/IWO',
]


def import_order_files(filepaths):
    """filepaths: list path file .xlsx order (boleh lebih dari 1, beda periode).
    Order yang No Invoice-nya udah terisi ATAU line-nya CANCELLED, di-skip.
    Return: jumlah order unik yang masuk pool."""
    orders_by_no = {}  # order_no -> Order object (belum di-commit)
    n_cancelled = 0
    n_invoiced = 0

    for fpath in filepaths:
        wb = openpyxl.load_workbook(fpath, data_only=True)
        ws = wb[wb.sheetnames[0]]
        headers = [ws.cell(row=5, column=c).value for c in range(1, ws.max_column + 1)]
        idx = {h: i + 1 for i, h in enumerate(headers) if h}
        missing = [c for c in REQUIRED_ORDER_COLUMNS if c not in idx]
        if missing:
            raise ValueError(f"File {fpath} kehilangan kolom: {missing}")

        for r in range(6, ws.max_row + 1):
            ono_raw = ws.cell(row=r, column=idx['Nomor Order']).value
            if ono_raw is None:
                continue
            order_no = str(ono_raw).strip().lstrip("'")

            inv = ws.cell(row=r, column=idx['No Invoice']).value
            if inv not in (None, ''):
                n_invoiced += 1
                continue

            line_status = (ws.cell(row=r, column=idx['Line Order Status']).value or '').strip().upper()
            if line_status == 'CANCELLED':
                n_cancelled += 1
                continue

            cust = ws.cell(row=r, column=idx['Nama Customer']).value
            odate = ws.cell(row=r, column=idx['Order Created Date']).value
            fee = float(ws.cell(row=r, column=idx['Total Fee']).value or 0)
            ppn = float(ws.cell(row=r, column=idx['PPN Amount']).value or 0)
            line_no = ws.cell(row=r, column=idx['Line Number']).value
            bentuk = ws.cell(row=r, column=idx['Bentuk Perikatan']).value
            oc_no = ws.cell(row=r, column=idx['Nomor OC/Kontrak/IWO']).value

            if order_no not in orders_by_no:
                orders_by_no[order_no] = {
                    'order_no': order_no, 'customer_raw': cust, 'customer_norm': norm_name(cust),
                    'order_date': odate.date() if hasattr(odate, 'date') else odate,
                    'source_file': fpath.split('/')[-1], 'lines': [],
                }
            o = orders_by_no[order_no]
            if odate and (o['order_date'] is None or odate.date() < o['order_date']):
                o['order_date'] = odate.date() if hasattr(odate, 'date') else odate
            o['lines'].append({
                'line_no': line_no, 'fee': fee, 'ppn': ppn,
                'bentuk_perikatan': bentuk, 'nomor_oc_kontrak': oc_no, 'line_status': line_status,
            })

    # tulis ke DB
    count = 0
    for order_no, data in orders_by_no.items():
        if not data['lines']:
            continue
        total_fee = sum(l['fee'] for l in data['lines'])
        total_ppn = sum(l['ppn'] for l in data['lines'])
        order = Order(
            order_no=order_no, customer_raw=data['customer_raw'], customer_norm=data['customer_norm'],
            order_date=data['order_date'], total_fee=total_fee, total_ppn=total_ppn,
            total_value=total_fee + total_ppn, source_file=data['source_file'], is_invoiced=False,
        )
        for l in data['lines']:
            order.lines.append(OrderLine(**l))
        db.session.add(order)
        count += 1

    db.session.commit()
    return {'orders_imported': count, 'lines_cancelled_skipped': n_cancelled, 'lines_invoiced_skipped': n_invoiced}


def import_ump_file(fpath, sheet_name=None, anomali_min_nominal=10000):
    """fpath: path file .xlsx UMP (kolom: NO, NAMA PELANGGAN, BUKTI, TANGGAL, ..., DALAM RUPIAH).
    Return: jumlah UMP yang berhasil diimport."""
    wb = openpyxl.load_workbook(fpath, data_only=True)
    if sheet_name:
        ws = wb[sheet_name]
    else:
        # cari sheet yang namanya mengandung "UMP" (case-insensitive) - file sumbernya
        # kadang workbook besar dengan banyak sheet lain, bukan cuma data UMP doang
        ump_sheets = [s for s in wb.sheetnames if 'UMP' in s.upper()]
        ws = wb[ump_sheets[0]] if ump_sheets else wb[wb.sheetnames[0]]

    count = 0
    for r in range(3, ws.max_row + 1):
        no = ws.cell(row=r, column=2).value
        if no is None:
            continue
        nama = ws.cell(row=r, column=3).value
        bukti = ws.cell(row=r, column=4).value
        tgl = ws.cell(row=r, column=5).value
        nominal = ws.cell(row=r, column=8).value
        if nominal is None:
            continue
        nominal = float(nominal)
        rec = UMPRecord(
            no_urut=no, nama_raw=nama, nama_norm=norm_name(nama), no_bukti=bukti,
            tanggal=tgl.date() if hasattr(tgl, 'date') else tgl, nominal=nominal,
            is_anomali=nominal < anomali_min_nominal,
        )
        db.session.add(rec)
        count += 1
    db.session.commit()
    return count
