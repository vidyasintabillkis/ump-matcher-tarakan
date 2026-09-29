"""Engine matching UMP <-> Order berbasis in-memory (tanpa database).
Semua data diproses di RAM dan langsung menghasilkan hasil pencocokan.
"""
from itertools import combinations
from collections import defaultdict, Counter
from datetime import datetime, date
import openpyxl
from rapidfuzz import fuzz, process

from matcher.norm_utils import norm_name


def combined_score(a, b, score_cutoff=0):
    if not a or not b:
        return 0.0
    return max(fuzz.token_sort_ratio(a, b), fuzz.token_set_ratio(a, b))


def word_similar(w1, w2):
    if w1 == w2:
        return True
    if len(w1) >= 4 and len(w2) >= 4 and (w1.startswith(w2) or w2.startswith(w1)):
        return True
    return False


def date_delta(d1, d2):
    if not d1 or not d2:
        return 999
    if isinstance(d1, datetime):
        d1 = d1.date()
    if isinstance(d2, datetime):
        d2 = d2.date()
    return abs((d1 - d2).days)


def candidate_values(fee, ppn):
    total = fee + ppn
    return {
        'fee_saja': fee,
        'total(fee+ppn)': total,
        'total-pph2%': total - 0.02 * fee,
        'fee-pph2%': fee * 0.98,
    }


def clamp(v, lo=1, hi=100):
    return max(lo, min(hi, round(v)))


def to_date(val):
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    if isinstance(val, str):
        for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y', '%Y/%m/%d'):
            try:
                return datetime.strptime(val.strip(), fmt).date()
            except ValueError:
                pass
    return None


# ---------------- PARSER EXCEL IN-MEMORY ----------------
def parse_ump_file(file_obj, anomali_min_nominal=10000):
    """Membaca file UMP dari file-like object (in-memory) tanpa simpan ke disk."""
    wb = openpyxl.load_workbook(file_obj, data_only=True)
    ump_sheets = [s for s in wb.sheetnames if 'UMP' in s.upper()]
    ws = wb[ump_sheets[0]] if ump_sheets else wb[wb.sheetnames[0]]

    umps = []
    # Mencari header atau langsung baca mulai baris ke-3
    for r in range(3, ws.max_row + 1):
        no = ws.cell(row=r, column=2).value
        nama = ws.cell(row=r, column=3).value
        bukti = ws.cell(row=r, column=4).value
        tgl = ws.cell(row=r, column=5).value
        nominal_raw = ws.cell(row=r, column=8).value

        if no is None and nama is None and nominal_raw is None:
            continue
        if nominal_raw is None:
            continue

        try:
            nominal = float(nominal_raw)
        except (ValueError, TypeError):
            continue

        nama_clean = str(nama).strip() if nama else ''
        umps.append({
            'id': len(umps) + 1,
            'no_urut': int(no) if str(no).isdigit() else len(umps) + 1,
            'nama_raw': nama_clean,
            'nama_norm': norm_name(nama_clean),
            'no_bukti': str(bukti).strip() if bukti else '',
            'tanggal': to_date(tgl),
            'nominal': nominal,
            'is_anomali': nominal < anomali_min_nominal,
        })
    return umps


def parse_order_files(file_objects):
    """Membaca satu atau banyak file order Excel dari memory stream."""
    orders_by_no = {}
    n_cancelled = 0
    n_invoiced = 0

    required_keys = ['Nomor Order', 'Nama Customer', 'Order Created Date', 'Total Fee',
                     'PPN Amount', 'No Invoice', 'Line Order Status', 'Line Number',
                     'Bentuk Perikatan', 'Nomor OC/Kontrak/IWO']

    for file_obj in file_objects:
        wb = openpyxl.load_workbook(file_obj, data_only=True)
        ws = wb[wb.sheetnames[0]]

        # Deteksi baris header (biasanya baris 5, atau scan 10 baris pertama)
        header_row = 5
        headers = []
        for r in range(1, min(15, ws.max_row + 1)):
            row_vals = [ws.cell(row=r, column=c).value for c in range(1, ws.max_column + 1)]
            if any('Nomor Order' in str(v) for v in row_vals if v):
                header_row = r
                headers = row_vals
                break

        if not headers:
            headers = [ws.cell(row=header_row, column=c).value for c in range(1, ws.max_column + 1)]

        idx = {str(h).strip(): i + 1 for i, h in enumerate(headers) if h}
        missing = [c for c in required_keys if c not in idx]
        if missing:
            raise ValueError(f"File order kehilangan kolom wajib: {', '.join(missing)}")

        for r in range(header_row + 1, ws.max_row + 1):
            ono_raw = ws.cell(row=r, column=idx['Nomor Order']).value
            if ono_raw is None:
                continue
            order_no = str(ono_raw).strip().lstrip("'")

            inv = ws.cell(row=r, column=idx['No Invoice']).value
            if inv not in (None, ''):
                n_invoiced += 1
                continue

            line_status = (str(ws.cell(row=r, column=idx['Line Order Status']).value or '')).strip().upper()
            if line_status == 'CANCELLED':
                n_cancelled += 1
                continue

            cust = str(ws.cell(row=r, column=idx['Nama Customer']).value or '').strip()
            odate_val = ws.cell(row=r, column=idx['Order Created Date']).value
            odate = to_date(odate_val)

            try:
                fee = float(ws.cell(row=r, column=idx['Total Fee']).value or 0)
            except (ValueError, TypeError):
                fee = 0.0
            try:
                ppn = float(ws.cell(row=r, column=idx['PPN Amount']).value or 0)
            except (ValueError, TypeError):
                ppn = 0.0

            line_no = ws.cell(row=r, column=idx['Line Number']).value
            bentuk = ws.cell(row=r, column=idx['Bentuk Perikatan']).value
            oc_no = ws.cell(row=r, column=idx['Nomor OC/Kontrak/IWO']).value

            if order_no not in orders_by_no:
                orders_by_no[order_no] = {
                    'order_no': order_no,
                    'customer_raw': cust,
                    'customer_norm': norm_name(cust),
                    'order_date': odate,
                    'lines': [],
                }
            o = orders_by_no[order_no]
            if odate and (o['order_date'] is None or odate < o['order_date']):
                o['order_date'] = odate

            o['lines'].append({
                'line_no': line_no,
                'fee': fee,
                'ppn': ppn,
                'bentuk_perikatan': str(bentuk).strip() if bentuk else '',
                'nomor_oc_kontrak': str(oc_no).strip() if oc_no else '',
                'line_status': line_status,
            })

    orders = []
    for order_no, data in orders_by_no.items():
        if not data['lines']:
            continue
        total_fee = sum(l['fee'] for l in data['lines'])
        total_ppn = sum(l['ppn'] for l in data['lines'])
        orders.append({
            'order_no': order_no,
            'customer_raw': data['customer_raw'],
            'customer_norm': data['customer_norm'],
            'date': data['order_date'],
            'total_fee': total_fee,
            'total_ppn': total_ppn,
            'value': total_fee + total_ppn,
            'lines': data['lines'],
        })

    return orders, {'cancelled_skipped': n_cancelled, 'invoiced_skipped': n_invoiced}


# ---------------- IN-MEMORY MATCHING ENGINE ----------------
class InMemoryMatchingEngine:
    def __init__(self, orders, config=None):
        cfg = config or {}
        self.date_window = cfg.get('DATE_WINDOW_DAYS', 60)
        self.amount_tol = cfg.get('AMOUNT_TOL_RP', 2000)
        self.amount_tol_pct = cfg.get('AMOUNT_TOL_PCT', 0.003)
        self.name_score_min = cfg.get('NAME_SCORE_MIN', 72)
        self.name_score_unique_override = cfg.get('NAME_SCORE_UNIQUE_OVERRIDE', 90)
        self.max_order_combo = cfg.get('MAX_ORDER_COMBO', 5)
        self.max_ump_combo = cfg.get('MAX_UMP_COMBO', 4)
        self.combo_pool_size = cfg.get('ORDER_COMBO_POOL', 15)
        self.freq_threshold = cfg.get('NOMINAL_UMUM_FREQ_THRESHOLD', 15)
        self.freq_date_threshold = cfg.get('NOMINAL_UMUM_DATE_THRESHOLD', 5)

        self.orders = orders
        self.orders_by_cust = defaultdict(list)
        self.word_to_custs = defaultdict(set)
        self.value_freq = Counter()

        for o in self.orders:
            if o['customer_norm']:
                self.orders_by_cust[o['customer_norm']].append(o)
            self.value_freq[round(o['value'])] += 1
            for l in o['lines']:
                self.value_freq[round(l['fee'])] += 1
                self.value_freq[round(l['fee'] + l['ppn'])] += 1

        self.cust_names = sorted(self.orders_by_cust.keys())
        for name in self.cust_names:
            for w in set(name.split()):
                self.word_to_custs[w].add(name)

    def close(self, a, b):
        return abs(a - b) <= max(self.amount_tol, abs(b) * self.amount_tol_pct)

    def date_ok(self, order_date, ump_date):
        return date_delta(order_date, ump_date) <= self.date_window

    def has_distinctive_overlap(self, a_norm, b_norm):
        if a_norm == b_norm:
            return True
        matched_pairs = [(aw, bw) for aw in a_norm.split() for bw in b_norm.split() if word_similar(aw, bw)]
        if not matched_pairs:
            return False
        distinctive = [bw for aw, bw in matched_pairs if len(self.word_to_custs.get(bw, set())) == 1]
        return len(distinctive) >= 1 or len(matched_pairs) >= 2

    def _order_combo_candidates(self, ump, matched):
        """Cari kombinasi 2..N order yang jumlahnya = nominal 1 UMP.

        - Grup per customer (satu nama customer).
        - Grup gabungan lintas customer, untuk kasus nama customer yang sama
          tercatat dengan ejaan berbeda (kena penalti skor tambahan).
        """
        target = float(ump['nominal'])
        out, seen = [], set()
        score_of = {cn: ns for cn, ns, _ in matched}

        groups = [(orders, False) for _, _, orders in matched]
        if len(matched) > 1:
            groups.append(([o for _, _, orders in matched for o in orders], True))

        for group_orders, cross in groups:
            if len(group_orders) < 2:
                continue
            pool = sorted(group_orders, key=lambda o: date_delta(o['date'], ump['tanggal']))[:self.combo_pool_size]
            for k in range(2, min(self.max_order_combo, len(pool)) + 1):
                for combo in combinations(pool, k):
                    combo = sorted(combo, key=lambda o: o['order_no'])
                    if cross and len({o['customer_norm'] for o in combo}) < 2:
                        continue  # combo 1 customer sudah dicek di grup per-customer
                    key = tuple(sorted(o['order_no'] for o in combo))
                    fee_sum = sum(o['total_fee'] for o in combo)
                    ppn_sum = sum(o['total_ppn'] for o in combo)
                    for adj, val in candidate_values(fee_sum, ppn_sum).items():
                        if not self.close(val, target) or (key, adj) in seen:
                            continue
                        seen.add((key, adj))
                        name_score = min(score_of[o['customer_norm']] for o in combo)
                        dd = max(date_delta(o['date'], ump['tanggal']) for o in combo)
                        # penalti: makin banyak order digabung makin kecil keyakinannya
                        base = (name_score - 5 - 3 * (k - 2)
                                - (0 if adj == 'total(fee+ppn)' else 2)
                                - (5 if cross else 0))
                        custs = []
                        for o in combo:
                            if o['customer_raw'] not in custs:
                                custs.append(o['customer_raw'])
                        out.append({
                            'orders': [o['order_no'] for o in combo],
                            'order_customer': ' / '.join(custs),
                            'order_dates': [o['date'] for o in combo],
                            'value': val, 'name_score': name_score, 'date_delta': dd,
                            'type': f'combo{k}', 'adj': adj, 'score': base - dd * 0.25,
                            'line_no': None, 'bentuk': None, 'oc_no': None,
                            'cross_customer': cross,
                            'breakdown': [(o['order_no'], candidate_values(o['total_fee'], o['total_ppn'])[adj])
                                          for o in combo],
                        })
        return out

    def match_by_name(self, ump):
        candidates = []
        matched = []  # (cust_norm, name_score, order_dalam_window)
        name_matches = process.extract(ump['nama_norm'], self.cust_names, scorer=combined_score, limit=5)

        for cust_norm, name_score, _ in name_matches:
            if name_score < self.name_score_min:
                continue
            if not self.has_distinctive_overlap(ump['nama_norm'], cust_norm):
                continue

            cand_orders = [o for o in self.orders_by_cust.get(cust_norm, []) if self.date_ok(o['date'], ump['tanggal'])]
            if not cand_orders:
                continue
            matched.append((cust_norm, name_score, cand_orders))

            # Per line
            for o in cand_orders:
                for line in o['lines']:
                    for adj, val in candidate_values(line['fee'], line['ppn']).items():
                        if self.close(val, float(ump['nominal'])):
                            dd = date_delta(o['date'], ump['tanggal'])
                            score = name_score - dd * 0.25
                            candidates.append({
                                'orders': [o['order_no']], 'order_customer': o['customer_raw'],
                                'order_dates': [o['date']], 'value': val, 'name_score': name_score,
                                'date_delta': dd, 'type': 'line', 'adj': adj, 'score': score,
                                'line_no': line['line_no'], 'bentuk': line['bentuk_perikatan'],
                                'oc_no': line['nomor_oc_kontrak'],
                            })

            # Per order total
            for o in cand_orders:
                for adj, val in candidate_values(o['total_fee'], o['total_ppn']).items():
                    if self.close(val, float(ump['nominal'])):
                        dd = date_delta(o['date'], ump['tanggal'])
                        base = name_score - (0 if adj == 'total(fee+ppn)' else 2)
                        score = base - dd * 0.25
                        candidates.append({
                            'orders': [o['order_no']], 'order_customer': o['customer_raw'],
                            'order_dates': [o['date']], 'value': val, 'name_score': name_score,
                            'date_delta': dd, 'type': 'order_total', 'adj': adj, 'score': score,
                            'line_no': None, 'bentuk': None, 'oc_no': None,
                        })

        # Multi-order combo: 1 pembayaran UMP untuk beberapa order sekaligus
        candidates.extend(self._order_combo_candidates(ump, matched))

        if not candidates:
            # Fallback jika nama sangat kuat dan hanya ada 1 order di window
            for cust_norm, name_score, _ in name_matches:
                if name_score >= self.name_score_unique_override:
                    cand_orders = [o for o in self.orders_by_cust.get(cust_norm, []) if self.date_ok(o['date'], ump['tanggal'])]
                    if len(cand_orders) == 1:
                        o = cand_orders[0]
                        candidates.append({
                            'orders': [o['order_no']], 'order_customer': o['customer_raw'],
                            'order_dates': [o['date']], 'value': o['value'], 'name_score': name_score,
                            'date_delta': date_delta(o['date'], ump['tanggal']),
                            'type': 'unique_no_amount', 'adj': 'fallback',
                            'score': name_score - date_delta(o['date'], ump['tanggal']) * 0.25,
                            'line_no': None, 'bentuk': None, 'oc_no': None,
                        })

        candidates.sort(key=lambda c: (
            -c['score'],
            0 if c['type'] in ('order_total', 'line') else 1,
            c['date_delta'],
            -c['value']
        ))
        return candidates

    def match_by_amount_only(self, ump):
        cands = []
        target = float(ump['nominal'])
        for o in self.orders:
            if not self.date_ok(o['date'], ump['tanggal']):
                continue
            for line in o['lines']:
                for adj, val in candidate_values(line['fee'], line['ppn']).items():
                    if self.close(val, target):
                        dd = date_delta(o['date'], ump['tanggal'])
                        freq = self.value_freq.get(round(val), 1)
                        cands.append({
                            'order': o, 'value': val, 'level': 'line', 'line_no': line['line_no'],
                            'bentuk': line['bentuk_perikatan'], 'oc_no': line['nomor_oc_kontrak'],
                            'adj': adj, 'date_delta': dd, 'freq': freq,
                        })
            for adj, val in candidate_values(o['total_fee'], o['total_ppn']).items():
                if self.close(val, target):
                    dd = date_delta(o['date'], ump['tanggal'])
                    freq = self.value_freq.get(round(val), 1)
                    cands.append({
                        'order': o, 'value': val, 'level': 'order_total', 'line_no': None,
                        'bentuk': None, 'oc_no': None, 'adj': adj, 'date_delta': dd, 'freq': freq,
                    })

        cands.sort(key=lambda c: (c['freq'], c['date_delta'], 0 if c['level'] == 'order_total' else 1))
        return cands

    def match_multi_ump_combo(self, umps):
        hits = []
        for o in self.orders:
            cands = []
            for u in umps:
                if not self.date_ok(o['date'], u['tanggal']):
                    continue
                score = combined_score(u['nama_norm'], o['customer_norm'])
                if score >= self.name_score_min:
                    cands.append((u, score, date_delta(o['date'], u['tanggal'])))

            if len(cands) < 2:
                continue

            target_values = list(candidate_values(o['total_fee'], o['total_ppn']).values())
            for line in o['lines']:
                target_values += list(candidate_values(line['fee'], line['ppn']).values())
            target_values = list(set(round(v) for v in target_values))

            found = []
            for k in range(2, min(self.max_ump_combo, len(cands)) + 1):
                for combo in combinations(cands, k):
                    total_nominal = sum(float(c[0]['nominal']) for c in combo)
                    for tv in target_values:
                        if self.close(total_nominal, tv):
                            found.append({
                                'order': o, 'value_matched': tv, 'ump_combo': [c[0] for c in combo],
                                'max_date_delta': max(c[2] for c in combo), 'name_scores': [c[1] for c in combo],
                            })
            if found:
                found.sort(key=lambda x: (len(x['ump_combo']), x['max_date_delta']))
                hits.append(found[0])
        return hits

    def match_all(self, umps):
        combo_hits = self.match_multi_ump_combo([u for u in umps if not u['is_anomali']])
        combo_by_ump_id = {}
        for h in combo_hits:
            for u in h['ump_combo']:
                combo_by_ump_id[u['id']] = h

        strongly_claimed = set()
        per_ump_candidates = {}

        for u in umps:
            if u['is_anomali'] or u['id'] in combo_by_ump_id:
                continue
            cands = self.match_by_name(u)
            per_ump_candidates[u['id']] = ('name', cands)
            if cands and cands[0]['type'] != 'unique_no_amount':
                for ono in cands[0]['orders']:
                    strongly_claimed.add(ono)

        for u in umps:
            u_id = u['id']
            if u_id in per_ump_candidates and per_ump_candidates[u_id][1]:
                cands = per_ump_candidates[u_id][1]
                if cands[0]['type'] == 'unique_no_amount' and cands[0]['orders'][0] in strongly_claimed:
                    remaining = [c for c in cands if not (c['type'] == 'unique_no_amount' and c['orders'][0] in strongly_claimed)]
                    per_ump_candidates[u_id] = ('name', remaining)

        for u in umps:
            if u['is_anomali'] or u['id'] in combo_by_ump_id:
                continue
            has_name = per_ump_candidates.get(u['id'], (None, []))[1]
            if not has_name:
                amount_cands = self.match_by_amount_only(u)
                per_ump_candidates[u['id']] = ('amount', amount_cands)

        results = []
        for u in umps:
            res = {
                'no_urut': u['no_urut'],
                'nama_ump': u['nama_raw'],
                'tanggal_ump': u['tanggal'],
                'no_bukti': u['no_bukti'],
                'nominal_ump': u['nominal'],
                'is_anomali': u['is_anomali'],
                'order_no': '',
                'customer_order': '',
                'tanggal_order': None,
                'selisih_hari': None,
                'nilai_cocok': None,
                'confidence': 0,
                'sumber': '',
                'tipe': '',
                'bentuk_oc': '',
                'kandidat_lain': '',
                'catatan': '',
                'jumlah_order': 0,
                'rincian_order': '',
                'rincian_items': [],
            }

            if u['is_anomali']:
                res['sumber'] = 'Data anomali'
                res['confidence'] = 0
                res['catatan'] = f"Nominal Rp{float(u['nominal']):,.0f} tidak wajar untuk uang muka (< batas minimum)"
                results.append(res)
                continue

            if u['id'] in combo_by_ump_id:
                h = combo_by_ump_id[u['id']]
                other_names = [x['nama_raw'].strip() for x in h['ump_combo'] if x['id'] != u['id']]
                total_gabungan = sum(float(x['nominal']) for x in h['ump_combo'])
                conf = clamp(min(h['name_scores']) - h['max_date_delta'] * 0.25 - (len(h['ump_combo']) - 2) * 3)
                res.update({
                    'order_no': h['order']['order_no'],
                    'customer_order': h['order']['customer_raw'],
                    'tanggal_order': h['order']['date'],
                    'selisih_hari': h['max_date_delta'],
                    'nilai_cocok': h['value_matched'],
                    'confidence': conf,
                    'jumlah_order': 1,
                    'sumber': 'Gabungan Beberapa UMP',
                    'tipe': 'combo_ump',
                    'catatan': f"GABUNGAN {len(h['ump_combo'])} UMP jadi 1 pembayaran (total Rp{total_gabungan:,.0f}) bersama: {', '.join(other_names)}",
                })
                results.append(res)
                continue

            kind, cands = per_ump_candidates.get(u['id'], (None, []))
            if kind == 'name' and cands:
                top = cands[0]
                alt = ', '.join(' + '.join(c['orders']) for c in cands[1:5])
                if top['type'] == 'unique_no_amount':
                    conf = clamp(min(top['name_score'] - top['date_delta'] * 0.25, 75))
                    catatan = 'NOMINAL BELUM DICEK - hanya nama+tanggal, cuma 1 order kandidat di window'
                else:
                    # combo: pakai skor yang sudah kena penalti jumlah order; lainnya tetap
                    conf = clamp(top['score'] if top['type'].startswith('combo')
                                 else top['name_score'] - top['date_delta'] * 0.25)
                    catatan = ''
                bentuk_info = f"{top.get('bentuk') or ''} {top.get('oc_no') or ''}".strip()

                is_combo = top['type'].startswith('combo')
                sumber = 'Nama+Nominal'
                rincian, items = '', []
                if is_combo:
                    items = list(top['breakdown'])
                    rincian = ' + '.join(f"{ono} (Rp{v:,.0f})" for ono, v in items)
                    sumber = 'Nama+Nominal (Bayar Banyak Order)'
                    catatan = f"1 UMP dipakai bayar {len(top['orders'])} order sekaligus: {rincian}"
                    if top.get('cross_customer'):
                        catatan += ' | Nama customer antar order berbeda ejaan, cek manual'

                res.update({
                    'jumlah_order': len(top['orders']),
                    'rincian_order': rincian,
                    'rincian_items': items,
                    'order_no': ', '.join(top['orders']),
                    'customer_order': top['order_customer'],
                    'tanggal_order': top['order_dates'][0] if top['order_dates'] else None,
                    'selisih_hari': top['date_delta'],
                    'nilai_cocok': top['value'],
                    'confidence': conf,
                    'sumber': sumber,
                    'tipe': f"{top['type']}/{top['adj']}",
                    'bentuk_oc': bentuk_info,
                    'kandidat_lain': alt,
                    'catatan': catatan,
                })
            elif kind == 'amount' and cands:
                usable = [c for c in cands if not (c['freq'] > self.freq_threshold and c['date_delta'] > self.freq_date_threshold)]
                if usable:
                    top = usable[0]
                    alt = ', '.join(c['order']['order_no'] for c in usable[1:5])
                    conf = clamp(65 - 5 * (top['freq'] - 1) - top['date_delta'] * 0.15)
                    bentuk_info = f"{top.get('bentuk') or ''} {top.get('oc_no') or ''}".strip()
                    res.update({
                        'jumlah_order': 1,
                        'order_no': top['order']['order_no'],
                        'customer_order': top['order']['customer_raw'],
                        'tanggal_order': top['order']['date'],
                        'selisih_hari': top['date_delta'],
                        'nilai_cocok': top['value'],
                        'confidence': conf,
                        'sumber': 'Nominal saja (nama beda)',
                        'tipe': f"{top['level']}/{top['adj']}",
                        'bentuk_oc': bentuk_info,
                        'kandidat_lain': alt,
                    })
                else:
                    top = cands[0]
                    res.update({
                        'sumber': 'Nominal terlalu umum',
                        'confidence': 0,
                        'catatan': f"Nominal ini muncul {top['freq']}x di order lain (kemungkinan fee standar) - terlalu umum untuk ditebak",
                    })
            else:
                res.update({
                    'sumber': 'Tidak ada kandidat',
                    'confidence': 0,
                    'catatan': 'Tidak ada order yang cocok (nama maupun nominal+tanggal) dalam pool data yang diunggah',
                })

            results.append(res)
        return results
