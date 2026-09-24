"""Engine matching UMP <-> Order. Port dari hasil tuning manual (lihat riwayat chat) -
semua parameter penting ada di config.py, jangan hardcode angka baru di sini kalau bisa dihindari."""
from itertools import combinations
from collections import defaultdict, Counter
from rapidfuzz import fuzz, process

from models import db, Order, OrderLine, UMPRecord, MatchResult, PayerAlias
from flask import current_app


def combined_score(a, b, score_cutoff=0):
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


class MatchingEngine:
    def __init__(self, cfg=None):
        cfg = cfg or current_app.config
        self.date_window = cfg['DATE_WINDOW_DAYS']
        self.amount_tol = cfg['AMOUNT_TOL_RP']
        self.amount_tol_pct = cfg['AMOUNT_TOL_PCT']
        self.name_score_min = cfg['NAME_SCORE_MIN']
        self.name_score_unique_override = cfg['NAME_SCORE_UNIQUE_OVERRIDE']
        self.max_order_combo = cfg['MAX_ORDER_COMBO']
        self.max_ump_combo = cfg['MAX_UMP_COMBO']
        self.freq_threshold = cfg['NOMINAL_UMUM_FREQ_THRESHOLD']
        self.freq_date_threshold = cfg['NOMINAL_UMUM_DATE_THRESHOLD']

        self.orders = []          # list of dict, order pool (belum invoice)
        self.orders_by_cust = defaultdict(list)
        self.cust_names = []
        self.word_to_custs = defaultdict(set)
        self.value_freq = Counter()

    # ---------- load ----------
    def load_orders(self):
        orders = Order.query.filter_by(is_invoiced=False).all()
        for o in orders:
            lines = [{
                'fee': float(l.fee or 0), 'ppn': float(l.ppn or 0), 'line_no': l.line_no,
                'bentuk': l.bentuk_perikatan, 'oc_no': l.nomor_oc_kontrak,
            } for l in o.lines]
            if not lines:
                continue
            d = {
                'order_no': o.order_no, 'customer_raw': o.customer_raw, 'customer_norm': o.customer_norm,
                'date': o.order_date, 'lines': lines,
                'total_fee': float(o.total_fee or 0), 'total_ppn': float(o.total_ppn or 0),
                'value': float(o.total_value or 0),
            }
            self.orders.append(d)
            if d['customer_norm']:
                self.orders_by_cust[d['customer_norm']].append(d)
        self.cust_names = sorted(self.orders_by_cust.keys())
        for name in self.cust_names:
            for w in set(name.split()):
                self.word_to_custs[w].add(name)
        for o in self.orders:
            self.value_freq[round(o['value'])] += 1
            for l in o['lines']:
                self.value_freq[round(l['fee'])] += 1
                self.value_freq[round(l['fee'] + l['ppn'])] += 1

    def has_distinctive_overlap(self, a_norm, b_norm):
        if a_norm == b_norm:
            return True
        matched_pairs = [(aw, bw) for aw in a_norm.split() for bw in b_norm.split() if word_similar(aw, bw)]
        if not matched_pairs:
            return False
        distinctive = [bw for aw, bw in matched_pairs if len(self.word_to_custs.get(bw, set())) == 1]
        return len(distinctive) >= 1 or len(matched_pairs) >= 2

    def close(self, a, b):
        return abs(a - b) <= max(self.amount_tol, abs(b) * self.amount_tol_pct)

    def date_ok(self, order_date, ump_date):
        return date_delta(order_date, ump_date) <= self.date_window

    # ---------- layer 1 : nama + nominal ----------
    def match_by_name(self, ump):
        """Return list kandidat (dict), urut skor descending. Cek payer_alias dulu."""
        candidates = []

        alias = PayerAlias.query.filter_by(payer_name_norm=ump.nama_norm).first()
        forced_cust_norm = alias.customer_norm if alias else None

        name_matches = process.extract(ump.nama_norm, self.cust_names, scorer=combined_score, limit=5)
        pool = [(forced_cust_norm, 100.0, None)] if forced_cust_norm else []
        pool += [m for m in name_matches if m[0] != forced_cust_norm]

        for cust_norm, name_score, _ in pool:
            if cust_norm != forced_cust_norm and name_score < self.name_score_min:
                continue
            if cust_norm != forced_cust_norm and not self.has_distinctive_overlap(ump.nama_norm, cust_norm):
                continue
            cand_orders = [o for o in self.orders_by_cust.get(cust_norm, []) if self.date_ok(o['date'], ump.tanggal)]
            if not cand_orders:
                continue

            for o in cand_orders:
                for line in o['lines']:
                    for adj, val in candidate_values(line['fee'], line['ppn']).items():
                        if self.close(val, float(ump.nominal)):
                            dd = date_delta(o['date'], ump.tanggal)
                            score = name_score - dd * 0.25
                            candidates.append(dict(orders=[o['order_no']], order_customer=o['customer_raw'],
                                order_dates=[o['date']], value=val, name_score=name_score, date_delta=dd,
                                type='line', adj=adj, score=score, line_no=line['line_no'],
                                bentuk=line['bentuk'], oc_no=line['oc_no']))
            for o in cand_orders:
                for adj, val in candidate_values(o['total_fee'], o['total_ppn']).items():
                    if self.close(val, float(ump.nominal)):
                        dd = date_delta(o['date'], ump.tanggal)
                        base = name_score - (0 if adj == 'total(fee+ppn)' else 2)
                        score = base - dd * 0.25
                        candidates.append(dict(orders=[o['order_no']], order_customer=o['customer_raw'],
                            order_dates=[o['date']], value=val, name_score=name_score, date_delta=dd,
                            type='order_total', adj=adj, score=score, line_no=None, bentuk=None, oc_no=None))
            if len(cand_orders) > 1:
                pool_orders = sorted(cand_orders, key=lambda o: date_delta(o['date'], ump.tanggal))[:15]
                for k in range(2, min(self.max_order_combo, len(pool_orders)) + 1):
                    for combo in combinations(pool_orders, k):
                        fee_sum = sum(o['total_fee'] for o in combo)
                        ppn_sum = sum(o['total_ppn'] for o in combo)
                        for adj, val in candidate_values(fee_sum, ppn_sum).items():
                            if self.close(val, float(ump.nominal)):
                                dd = max(date_delta(o['date'], ump.tanggal) for o in combo)
                                base = name_score - 5 - (0 if adj == 'total(fee+ppn)' else 2)
                                score = base - dd * 0.25
                                candidates.append(dict(orders=[o['order_no'] for o in combo],
                                    order_customer=combo[0]['customer_raw'], order_dates=[o['date'] for o in combo],
                                    value=val, name_score=name_score, date_delta=dd, type=f'combo{k}', adj=adj,
                                    score=score, line_no=None, bentuk=None, oc_no=None))
            if name_score >= self.name_score_unique_override and len(cand_orders) == 1:
                o = cand_orders[0]
                already = any(c['orders'] == [o['order_no']] for c in candidates)
                if not already:
                    dd = date_delta(o['date'], ump.tanggal)
                    score = (name_score - 10) - dd * 0.25
                    candidates.append(dict(orders=[o['order_no']], order_customer=o['customer_raw'],
                        order_dates=[o['date']], value=o['value'], name_score=name_score, date_delta=dd,
                        type='unique_no_amount', adj='n/a', score=score, line_no=None, bentuk=None, oc_no=None))

        candidates.sort(key=lambda c: -c['score'])
        seen, deduped = set(), []
        for c in candidates:
            key = tuple(sorted(c['orders']))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(c)
        return deduped[:6]

    # ---------- layer 2 : nominal + tanggal doang (nama gagal) ----------
    def match_by_amount_only(self, ump):
        results = []
        for o in self.orders:
            dd = date_delta(o['date'], ump.tanggal)
            if dd > self.date_window:
                continue
            for line in o['lines']:
                for adj, val in candidate_values(line['fee'], line['ppn']).items():
                    if self.close(val, float(ump.nominal)):
                        freq = self.value_freq.get(round(val), 1)
                        results.append(dict(order=o, adj=adj, value=val, level='line', date_delta=dd,
                            freq=freq, line_no=line['line_no'], bentuk=line['bentuk'], oc_no=line['oc_no']))
            for adj, val in candidate_values(o['total_fee'], o['total_ppn']).items():
                if self.close(val, float(ump.nominal)):
                    freq = self.value_freq.get(round(val), 1)
                    results.append(dict(order=o, adj=adj, value=val, level='order_total', date_delta=dd,
                        freq=freq, line_no=None, bentuk=None, oc_no=None))
        results.sort(key=lambda c: (c['freq'], c['date_delta']))
        seen, uniq = set(), []
        for c in results:
            ono = c['order']['order_no']
            if ono in seen:
                continue
            seen.add(ono)
            uniq.append(c)
        return uniq

    # ---------- layer 3 : beberapa UMP digabung -> 1 order ----------
    def match_multi_ump_combo(self, umps):
        hits = []
        for o in self.orders:
            if not o['customer_norm']:
                continue
            cands = []
            for u in umps:
                if u.is_anomali:
                    continue
                score = combined_score(u.nama_norm, o['customer_norm'])
                if score < self.name_score_min:
                    continue
                if not self.has_distinctive_overlap(u.nama_norm, o['customer_norm']):
                    continue
                dd = date_delta(o['date'], u.tanggal)
                if dd > self.date_window:
                    continue
                cands.append((u, score, dd))
            if len(cands) < 2:
                continue
            cands.sort(key=lambda x: x[2])
            cands = cands[:8]

            target_values = list(candidate_values(o['total_fee'], o['total_ppn']).values())
            for line in o['lines']:
                target_values += list(candidate_values(line['fee'], line['ppn']).values())
            target_values = list(set(round(v) for v in target_values))

            found = []
            for k in range(2, min(self.max_ump_combo, len(cands)) + 1):
                for combo in combinations(cands, k):
                    total_nominal = sum(float(c[0].nominal) for c in combo)
                    for tv in target_values:
                        if self.close(total_nominal, tv):
                            found.append(dict(order=o, value_matched=tv, ump_combo=[c[0] for c in combo],
                                max_date_delta=max(c[2] for c in combo), name_scores=[c[1] for c in combo]))
            if found:
                found.sort(key=lambda x: (len(x['ump_combo']), x['max_date_delta']))
                hits.append(found[0])
        return hits

    # ---------- jalanin semua & simpan ke DB ----------
    def run(self):
        self.load_orders()
        umps = UMPRecord.query.join(
            MatchResult, isouter=True
        ).filter(
            db.or_(MatchResult.id.is_(None), MatchResult.status == 'pending')
        ).all()

        combo_hits = self.match_multi_ump_combo([u for u in umps if not u.is_anomali])
        combo_claimed_orders = {h['order']['order_no'] for h in combo_hits}
        combo_by_ump_id = {}
        for h in combo_hits:
            for u in h['ump_combo']:
                combo_by_ump_id[u.id] = h

        strongly_claimed = set()
        per_ump_candidates = {}

        for u in umps:
            if u.is_anomali or u.id in combo_by_ump_id:
                continue
            cands = self.match_by_name(u)
            per_ump_candidates[u.id] = ('name', cands)
            if cands and cands[0]['type'] != 'unique_no_amount':
                for ono in cands[0]['orders']:
                    strongly_claimed.add(ono)

        for u in umps:
            if u.id in per_ump_candidates and per_ump_candidates[u.id][1]:
                cands = per_ump_candidates[u.id][1]
                if cands[0]['type'] == 'unique_no_amount' and cands[0]['orders'][0] in strongly_claimed:
                    remaining = [c for c in cands if not (c['type'] == 'unique_no_amount' and c['orders'][0] in strongly_claimed)]
                    per_ump_candidates[u.id] = ('name', remaining)

        for u in umps:
            if u.is_anomali or u.id in combo_by_ump_id:
                continue
            has_name_result = per_ump_candidates.get(u.id, (None, []))[1]
            if not has_name_result:
                amount_cands = self.match_by_amount_only(u)
                per_ump_candidates[u.id] = ('amount', amount_cands)

        # ---- tulis ke MatchResult ----
        for u in umps:
            existing = MatchResult.query.filter_by(ump_id=u.id).first()
            if existing and existing.status != 'pending':
                continue  # udah direview manual, jangan ditimpa
            if not existing:
                existing = MatchResult(ump_id=u.id)
                db.session.add(existing)

            if u.is_anomali:
                self._fill_result(existing, sumber='Data anomali', confidence=0,
                                   catatan=f'Nominal Rp{float(u.nominal):,.0f} tidak wajar untuk uang muka - dikeluarkan dari pencarian')
                continue

            if u.id in combo_by_ump_id:
                h = combo_by_ump_id[u.id]
                other_names = [x.nama_raw.strip() for x in h['ump_combo'] if x.id != u.id]
                total_gabungan = sum(float(x.nominal) for x in h['ump_combo'])
                confidence = clamp(min(h['name_scores']) - h['max_date_delta'] * 0.25 - (len(h['ump_combo']) - 2) * 3)
                self._fill_result(existing, sumber='Gabungan Beberapa UMP', tipe='combo_ump',
                    order_no=h['order']['order_no'], customer=h['order']['customer_raw'],
                    tanggal_order=h['order']['date'], selisih=h['max_date_delta'], nilai=h['value_matched'],
                    confidence=confidence, freq=self.value_freq.get(round(h['value_matched']), 1),
                    catatan=f"GABUNGAN {len(h['ump_combo'])} UMP jadi 1 pembayaran (total Rp{total_gabungan:,.0f}) bersama: {', '.join(other_names)}")
                continue

            kind, cands = per_ump_candidates.get(u.id, (None, []))
            if kind == 'name' and cands:
                top = cands[0]
                alt = ', '.join(', '.join(c['orders']) for c in cands[1:5])
                if top['type'] == 'unique_no_amount':
                    confidence = clamp(min(top['name_score'] - top['date_delta'] * 0.25, 75))
                    catatan = 'NOMINAL BELUM DICEK - hanya nama+tanggal, cuma 1 kandidat di window'
                else:
                    confidence = clamp(top['name_score'] - top['date_delta'] * 0.25)
                    catatan = ''
                bentuk_info = f"{top.get('bentuk') or ''} {top.get('oc_no') or ''}".strip()
                self._fill_result(existing, sumber='Nama+Nominal', tipe=f"{top['type']}/{top['adj']}",
                    order_no=', '.join(top['orders']), customer=top['order_customer'],
                    line_no=top.get('line_no'), bentuk=bentuk_info,
                    tanggal_order=top['order_dates'][0] if top['order_dates'] else None,
                    selisih=top['date_delta'], nilai=top['value'], confidence=confidence,
                    freq=self.value_freq.get(round(top['value']), 1), kandidat_lain=alt, catatan=catatan)
            elif kind == 'amount' and cands:
                usable = [c for c in cands if not (c['freq'] > self.freq_threshold and c['date_delta'] > self.freq_date_threshold)]
                if usable:
                    top = usable[0]
                    alt = ', '.join(c['order']['order_no'] for c in usable[1:5])
                    confidence = clamp(65 - 5 * (top['freq'] - 1) - top['date_delta'] * 0.15)
                    bentuk_info = f"{top.get('bentuk') or ''} {top.get('oc_no') or ''}".strip()
                    self._fill_result(existing, sumber='Nominal saja (nama gagal)', tipe=f"{top['level']}/{top['adj']}",
                        order_no=top['order']['order_no'], customer=top['order']['customer_raw'],
                        line_no=top.get('line_no'), bentuk=bentuk_info, tanggal_order=top['order']['date'],
                        selisih=top['date_delta'], nilai=top['value'], confidence=confidence,
                        freq=top['freq'], kandidat_lain=alt, catatan='')
                else:
                    top = cands[0]
                    self._fill_result(existing, sumber='Nominal terlalu umum', confidence=0, freq=top['freq'],
                        catatan=f"Nominal ini muncul {top['freq']}x di order lain (kemungkinan fee standar) - terlalu umum buat ditebak, perlu info tambahan")
            else:
                self._fill_result(existing, sumber='Tidak ada kandidat', confidence=0,
                                   catatan='Tidak ada order yang cocok (nama maupun nominal+tanggal) dalam pool yang ada')

        db.session.commit()
        return {'total_ump_diproses': len(umps), 'total_order_pool': len(self.orders)}

    @staticmethod
    def _fill_result(mr, sumber, tipe=None, order_no=None, customer=None, line_no=None, bentuk=None,
                      tanggal_order=None, selisih=None, nilai=None, confidence=0, freq=None,
                      kandidat_lain=None, catatan=None):
        mr.sumber = sumber
        mr.tipe_kecocokan = tipe
        mr.order_no = order_no
        mr.customer_kandidat = customer
        mr.line_no = line_no
        mr.bentuk_oc = bentuk
        mr.tanggal_order = tanggal_order
        mr.selisih_hari = selisih
        mr.nilai_cocok = nilai
        mr.confidence = confidence
        mr.frekuensi_nominal = freq
        mr.kandidat_lain = kandidat_lain
        mr.catatan = catatan
        mr.status = 'pending'
