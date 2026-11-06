"""Validação da data da intervenção lida no carimbo/EXIF.

A regra de leitura não muda (carimbo ou EXIF; nunca a data do arquivo). Aqui só se
recusa o que é impossível: dia/mês inexistente (33/10), ano antes de 2020 ou data
depois do dia em que a foto entrou no sistema (OCR leu 2028, 2086, 2626…).
"""
import datetime as dt

ANO_MIN = 2020


def _dt(s):
    try:
        return dt.datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def data_valida(data, referencia=None):
    d = _dt(data)
    if d is None or d.year < ANO_MIN:
        return False
    ref = _dt(referencia) if isinstance(referencia, str) else referencia
    ref = ref or dt.datetime.now()
    return d <= ref + dt.timedelta(days=1)


def fmt_br(data):
    """'2026-10-33T14:16:00' -> '33/10/2026 14:16' (mesmo que a data seja impossível)."""
    if not data or len(data) < 10:
        return data or ""
    return f"{data[8:10]}/{data[5:7]}/{data[0:4]}" + (f" {data[11:16]}" if len(data) >= 16 else "")
