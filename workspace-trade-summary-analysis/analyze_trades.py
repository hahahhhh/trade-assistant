#!/usr/bin/env python3
"""Reproducible post-trade audit for the real trade ledger."""
from __future__ import annotations
import argparse, csv, json, math
from collections import defaultdict
from pathlib import Path


def num(row, key):
    try: return float(row[key]) if row.get(key, '').strip() else None
    except ValueError: return None

def group_stats(rows):
    n=len(rows); wins=sum((num(r,'pnl_points') or 0)>0 for r in rows)
    pnl=[num(r,'pnl_points') for r in rows if num(r,'pnl_points') is not None]
    risk=[num(r,'risk_points') for r in rows if num(r,'risk_points') not in (None,0)]
    rs=[p/r for r,p in zip(risk,pnl)] if len(risk)==len(pnl) else []
    return {'n':n,'wins':wins,'win_rate':wins/n if n else None,'sum_points':sum(pnl),'avg_points':sum(pnl)/len(pnl) if pnl else None,'avg_R':sum(rs)/len(rs) if rs else None}

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('ledger',type=Path); ap.add_argument('--out',type=Path,default=Path('workspace-trade-summary-analysis/data/trade_assistant/report.json')); args=ap.parse_args()
    rows=list(csv.DictReader(args.ledger.open(encoding='utf-8-sig')))
    overall=group_stats(rows); groups={}
    for key in ('variety','direction','smc_tag','daily_rating','over_quota'):
        d=defaultdict(list)
        for r in rows: d[r.get(key,'(blank)') or '(blank)'].append(r)
        groups[key]={k:group_stats(v) for k,v in sorted(d.items())}
    violations=[]
    for r in rows:
        tags=[]; note=(r.get('note') or '').lower(); reason=(r.get('entry_reason') or '').lower()
        if str(r.get('over_quota','')).lower()=='true': tags.append('over_quota')
        if '拖损' in note or 'loosen' in note: tags.append('stop_discipline')
        if '加仓' in note or '补仓' in note: tags.append('add_on')
        if tags: violations.append({'trade_id':r.get('trade_id'),'tags':tags,'pnl_points':num(r,'pnl_points')})
    report={'source':str(args.ledger),'rows':len(rows),'overall':overall,'groups':groups,'heuristic_flags':violations,'limitations':['点数统计未含真实手续费/滑点，除非账本提供成本字段。','备注归因是候选标记，必须人工复核。','小样本分组不构成交易规则。']}
    args.out.parent.mkdir(parents=True,exist_ok=True); args.out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    md=args.out.with_suffix('.md'); lines=['# 交易账本审计报告','',f"- 数据源：`{args.ledger}`",f"- 样本数：{len(rows)}",'','## 总体', '']
    for k,v in overall.items(): lines.append(f'- {k}: {v}')
    lines += ['', '## 分组摘要','']
    for key, vals in groups.items():
      lines += [f'### {key}','','|分组|样本|胜率|总点数|平均R|','|---|---:|---:|---:|---:|']
      for name,s in vals.items(): lines.append(f"|{name}|{s['n']}|{s['win_rate']:.1%}|{s['sum_points']:.1f}|{s['avg_R'] if s['avg_R'] is not None else '—'}|")
      lines.append('')
    lines += ['## 候选纪律标记','', '|交易|标记|点数|','|---:|---|---:|']
    for x in violations: lines.append(f"|{x['trade_id']}|{', '.join(x['tags'])}|{x['pnl_points']}|")
    lines += ['', '## 解释边界','']+ [f'- {x}' for x in report['limitations']]
    md.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(md); print(args.out)
if __name__=='__main__': main()
