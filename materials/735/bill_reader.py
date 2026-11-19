"""合成需量账单的只读客户，不计算积分或搜索最优窗。"""
import argparse,json
from pathlib import Path
from fractions import Fraction
def display(x):
    return '-' if x is None else str(Fraction(x['n'],x['d']))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('bill');a=p.parse_args();r=json.loads(Path(a.bill).read_text())
    if r['schema']!='demand-bill/v1':raise ValueError('unsupported bill')
    w=r['window'];window='-' if w is None else display(w[0])+':'+display(w[1])
    print(r['settlement_id'],r['coverage'],window,display(r['demand']),display(r['charge']))
