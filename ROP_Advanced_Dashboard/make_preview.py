from pathlib import Path
import json
import numpy as np
import pandas as pd
from bokeh.embed import file_html
from bokeh.layouts import column, gridplot, row
from bokeh.models import ColumnDataSource, Div, HoverTool
from bokeh.plotting import figure
from bokeh.resources import INLINE
from bokeh.transform import cumsum

BASE=Path(__file__).resolve().parent
status=pd.read_csv(BASE/'data/fact_sku_status.csv.gz', low_memory=False)
branch=pd.read_csv(BASE/'data/fact_branch_rop.csv.gz', low_memory=False)
meta=json.loads((BASE/'data/metadata.json').read_text(encoding='utf-8'))

STATUS_ORDER=['Тасарсан','ROP-оос доош','Өгөгдөл алга','ROP байхгүй','Хэвийн','Илүүдэл']
STATUS_COLORS={'Тасарсан':'#B91C1C','ROP-оос доош':'#F97316','Өгөгдөл алга':'#7C3AED','ROP байхгүй':'#6B7280','Хэвийн':'#0F766E','Илүүдэл':'#2563EB'}
VED_COLORS={'V':'#B91C1C','E':'#F59E0B','D':'#0F766E','Тодорхойгүй':'#6B7280'}

header=Div(text='''<div style="font-family:Segoe UI,Arial;background:linear-gradient(90deg,#12263A,#1F4E5F);color:white;padding:20px 24px;border-radius:12px;">
<div style="font-size:28px;font-weight:750;">ROP & Нөөцийн удирдлагын ахисан түвшний дашбоард</div>
<div style="font-size:13px;opacity:.9;margin-top:5px;">Initial snapshot: 2026-08-13 | Python ETL + Power BI-ready model</div></div>''', sizing_mode='stretch_width', height=100)

def card(label,value,sub,accent):
    return f'''<div style="flex:1;min-width:180px;background:white;border:1px solid #E5E7EB;border-top:4px solid {accent};border-radius:10px;padding:12px 14px;box-shadow:0 1px 4px rgba(0,0,0,.06);">
<div style="font-size:12px;color:#6B7280;font-weight:600;">{label}</div><div style="font-size:26px;font-weight:750;color:#111827;margin-top:4px;">{value}</div><div style="font-size:11px;color:#6B7280;margin-top:3px;">{sub}</div></div>'''

kpis='<div style="display:flex;gap:12px;flex-wrap:wrap;font-family:Segoe UI,Arial;">'
kpis+=card('Нийт SKU',f"{meta['sku_count']:,}",'Master assortment','#1F4E5F')
kpis+=card('Inventory Position',f"{meta['on_hand_total_matched']:,.0f}",'Matched on hand','#2563EB')
kpis+=card('Total ROP',f"{meta['network_rop_total']:,.0f}",'Network working ROP','#0F766E')
kpis+=card('ROP gap',f"{meta['rop_gap_total']:,.0f}",'Replenishment gap','#F97316')
kpis+=card('Critical V/E',f"{meta['critical_ve_sku']:,}",'Below ROP or stockout','#B91C1C')
kpis+=card('Mapping rate',f"{meta['mapping']['mapping_rate_rows']:.1%}",f"{meta['mapping']['unmatched_rows']:,} unmatched rows",'#7C3AED')
kpis+='</div>'
kpi=Div(text=kpis,sizing_mode='stretch_width',height=140)

counts=status['Status'].value_counts().reindex(STATUS_ORDER,fill_value=0)
status_df=pd.DataFrame({'Status':counts.index,'Count':counts.values})
status_df['Angle']=status_df['Count']/max(status_df['Count'].sum(),1)*2*np.pi
status_df['Color']=status_df['Status'].map(STATUS_COLORS)
ss=ColumnDataSource(status_df)
p1=figure(title='Status бүтэц',height=370,sizing_mode='stretch_width',toolbar_location=None)
p1.annular_wedge(x=0,y=0,inner_radius=.55,outer_radius=.95,start_angle=cumsum('Angle',include_zero=True),end_angle=cumsum('Angle'),color='Color',legend_field='Status',source=ss)
p1.axis.visible=False;p1.grid.grid_line_color=None;p1.legend.location='center_right';p1.add_tools(HoverTool(tooltips=[('Status','@Status'),('SKU','@Count{0,0}')]))

ved_rows=[]
for ved in ['V','E','D','Тодорхойгүй']:
    sub=status[status['VED']==ved]
    ved_rows.append((ved,int(sub['Status'].isin(['Тасарсан','ROP-оос доош']).sum()),float(sub['ROPGap'].sum(skipna=True)),VED_COLORS[ved]))
ved_df=pd.DataFrame(ved_rows,columns=['VED','Critical','Gap','Color'])
vs=ColumnDataSource(ved_df)
p2=figure(title='VED эрсдэл',x_range=ved_df['VED'].tolist(),height=370,sizing_mode='stretch_width',toolbar_location=None)
p2.vbar(x='VED',top='Critical',width=.55,color='Color',source=vs)
p2.add_tools(HoverTool(tooltips=[('VED','@VED'),('Critical SKU','@Critical{0,0}'),('ROP gap','@Gap{0,0}')]))
p2.xgrid.grid_line_color=None

risk=status[status['Status'].isin(['Тасарсан','ROP-оос доош'])].sort_values(['WeightedGap','PriorityScore'],ascending=False).head(20).copy()
risk['Label']=risk['SKU_Name'].astype(str).str.slice(0,55)
risk['Color']=risk['VED'].map(VED_COLORS).fillna('#6B7280')
rs=ColumnDataSource(risk)
p3=figure(title='Нөхөх шаардлагатай TOP 20 SKU',y_range=risk['Label'].tolist()[::-1],height=540,sizing_mode='stretch_width',toolbar_location='above')
p3.hbar(y='Label',right='WeightedGap',height=.72,color='Color',source=rs)
p3.add_tools(HoverTool(tooltips=[('Нэр','@SKU_Name'),('VED','@VED'),('Status','@Status'),('On hand','@InventoryPosition{0,0.00}'),('ROP','@ROP{0,0}'),('Gap','@ROPGap{0,0.00}')]))
p3.xaxis.axis_label='VED-weighted ROP gap'

sc=status[status['ROP'].fillna(0)>0].copy()
sc['ROPPlot']=sc['ROP'].fillna(0).clip(lower=0)+1
sc['InvPlot']=sc['InventoryPosition'].fillna(0).clip(lower=0)+1
sc['Color']=sc['Status'].map(STATUS_COLORS).fillna('#6B7280')
scs=ColumnDataSource(sc)
p4=figure(title='Inventory Position vs ROP',x_axis_type='log',y_axis_type='log',height=500,sizing_mode='stretch_width',toolbar_location='above')
p4.scatter(x='ROPPlot',y='InvPlot',size=6,alpha=.5,color='Color',source=scs)
p4.line([1,100000],[1,100000],line_dash='dashed',line_color='#4B5563')
p4.add_tools(HoverTool(tooltips=[('Нэр','@SKU_Name'),('VED','@VED'),('Status','@Status'),('ROP','@ROP{0,0}'),('Inventory','@InventoryPosition{0,0.00}'),('Gap','@ROPGap{0,0.00}')]))
p4.xaxis.axis_label='ROP + 1';p4.yaxis.axis_label='Inventory Position + 1'

b=branch.groupby(['BranchName','VED'],as_index=False)['ROP'].sum().pivot(index='BranchName',columns='VED',values='ROP').fillna(0)
for c in ['V','E','D']:
    if c not in b: b[c]=0
b['Total']=b[['V','E','D']].sum(axis=1)
b=b.sort_values('Total',ascending=False).head(25).reset_index()
bs=ColumnDataSource(b)
p5=figure(title='Салбарын corrected ROP exposure',y_range=b['BranchName'].tolist()[::-1],height=620,sizing_mode='stretch_width',toolbar_location='above')
p5.hbar_stack(['V','E','D'],y='BranchName',height=.72,color=[VED_COLORS['V'],VED_COLORS['E'],VED_COLORS['D']],source=bs,legend_label=['V','E','D'])
p5.add_tools(HoverTool(tooltips=[('Салбар','@BranchName'),('V','@V{0,0}'),('E','@E{0,0}'),('D','@D{0,0}'),('Total','@Total{0,0}')]))
p5.legend.location='bottom_right'

note=Div(text='''<div style="font-family:Segoe UI,Arial;background:#F8FAFC;border-left:5px solid #1F4E5F;padding:14px 18px;border-radius:8px;line-height:1.5;">
<b>Автомат шинэчлэл:</b> Python dashboard-ийн app.py-г <code>bokeh serve --show app.py</code> командаар ажиллуулна. XLSX/XLSB/CSV үлдэгдлийн файл upload хийхэд alias mapping, ROP gap, VED risk, data-quality KPI шинэчлэгдэнэ. Power BI нь ижил star-schema CSV-үүдийг ашиглана.
</div>''',sizing_mode='stretch_width',height=90)

layout=column(header,kpi,gridplot([[p1,p2]],sizing_mode='stretch_width'),p3,p4,p5,note,sizing_mode='stretch_width')
html=file_html(layout,INLINE,'ROP Advanced Dashboard Preview')
(BASE/'dashboard_preview.html').write_text(html,encoding='utf-8')
print(BASE/'dashboard_preview.html')
