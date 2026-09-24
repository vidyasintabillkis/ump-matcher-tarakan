import re

ABBREV_MAP = {
    'RSUD': 'RUMAH SAKIT UMUM DAERAH',
    'RSU': 'RUMAH SAKIT UMUM',
    'PEMKAB': 'PEMERINTAH KABUPATEN',
    'PEMKOT': 'PEMERINTAH KOTA',
    'PEMDA': 'PEMERINTAH DAERAH',
    'PEMPROV': 'PEMERINTAH PROVINSI',
    'KAB': 'KABUPATEN',
    'KEC': 'KECAMATAN',
    'PROV': 'PROVINSI',
    'BPBD': 'BADAN PENANGGULANGAN BENCANA DAERAH',
    'DLH': 'DINAS LINGKUNGAN HIDUP',
    'DISHUB': 'DINAS PERHUBUNGAN',
    'DISDIK': 'DINAS PENDIDIKAN',
    'DISKES': 'DINAS KESEHATAN',
    'BPKAD': 'BADAN PENGELOLA KEUANGAN DAN ASET DAERAH',
    'SETDA': 'SEKRETARIAT DAERAH',
    'PUSK': 'PUSKESMAS',
    'KALTARA': 'KALIMANTAN UTARA',
    'KALTIM': 'KALIMANTAN TIMUR',
    'UPT': 'UNIT PELAKSANA TEKNIS',
}


def norm_name(s):
    if not s:
        return ''
    s = str(s).upper()
    s = re.sub(r'\bPERSEROAN TERBATAS\b', '', s)
    s = re.sub(r'\b(PT|CV|UD|TBK|PERSERO|PERUM|KOPERASI|IBU|BAPAK|BPK|SDR|SDRI|BADAN|BEND|BENDAHARA)\b\.?', '', s)
    s = re.sub(r'[^A-Z0-9 ]', ' ', s)
    words = s.split()
    words = [ABBREV_MAP.get(w, w) for w in words]
    s = ' '.join(words)
    s = re.sub(r'\s+', ' ', s).strip()
    return s
