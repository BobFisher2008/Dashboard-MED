from pathlib import Path
import json
import pandas as pd
import matplotlib.pyplot as plt
from PIL import Image, ImageDraw, ImageFont

BASE=Path(__file__).resolve().parent
assets=BASE/'assets'; assets.mkdir(exist_ok=True)
status=pd.read_csv(BASE/'data/fact_sku_status.csv.gz',low_memory=False)
branch=pd.read_csv(BASE/'data/fact_branch_rop.csv.gz',low_memory=False)
meta=json.loads((BASE/'data/metadata.json').read_text(encoding='utf-8'))

STATUS_ORDER=['Тасарсан','ROP-оос доош','Өгөгдөл алга','ROP байхгүй','Хэвийн','Илүүдэл']
STATUS_COLORS={'Тасарсан':'#B91C1C','ROP-оос доош':'#F97316','Өгөгдөл алга':'#7C3AED','ROP байхгүй':'#6B7280','Хэвийн':'#0F766E','Илүүдэл':'#2563EB'}
VED_COLORS={'V':'#B91C1C','E':'#F59E0B','D':'#0F766E','Тодорхойгүй':'#6B7280'}

# 1. Status donut
counts=status['Status'].value_counts().reindex(STATUS_ORDER,fill_value=0)
fig,ax=plt.subplots(figsize=(7.2,4.2))
ax.pie(counts.values,labels=counts.index,colors=[STATUS_COLORS[x] for x in counts.index],autopct=lambda p:f'{p:.1f}%' if p>=2 else '',startangle=90,wedgeprops={'width':0.42,'edgecolor':'white'})
ax.set_title('Status бүтэц',fontweight='bold')
fig.tight_layout();fig.savefig(assets/'status.png',dpi=160,bbox_inches='tight');plt.close(fig)

# 2. VED risk
rows=[]
for ved in ['V','E','D']:
    sub=status[status['VED']==ved]
    rows.append((ved,int(sub['Status'].isin(['Тасарсан','ROP-оос доош']).sum())))
veddf=pd.DataFrame(rows,columns=['VED','Critical'])
fig,ax=plt.subplots(figsize=(7.2,4.2))
ax.bar(veddf['VED'],veddf['Critical'],color=[VED_COLORS[x] for x in veddf['VED']])
for i,v in enumerate(veddf['Critical']): ax.text(i,v+max(5,v*.02),f'{v:,}',ha='center',fontweight='bold')
ax.set_title('VED critical SKU',fontweight='bold');ax.set_ylabel('SKU');ax.spines[['top','right']].set_visible(False)
fig.tight_layout();fig.savefig(assets/'ved.png',dpi=160,bbox_inches='tight');plt.close(fig)

# 3. Top shortage
risk=status[status['Status'].isin(['Тасарсан','ROP-оос доош'])].sort_values('WeightedGap',ascending=False).head(15).sort_values('WeightedGap')
fig,ax=plt.subplots(figsize=(10.5,6.2))
ax.barh(risk['SKU_Name'].astype(str).str.slice(0,48),risk['WeightedGap'],color=[VED_COLORS.get(x,'#6B7280') for x in risk['VED']])
ax.set_title('Нөхөх шаардлагатай TOP 15 SKU',fontweight='bold');ax.set_xlabel('VED-weighted ROP gap');ax.spines[['top','right']].set_visible(False)
fig.tight_layout();fig.savefig(assets/'shortage.png',dpi=160,bbox_inches='tight');plt.close(fig)

# 4. Branch exposure
b=branch.groupby(['BranchName','VED'],as_index=False)['ROP'].sum().pivot(index='BranchName',columns='VED',values='ROP').fillna(0)
for c in ['V','E','D']:
    if c not in b: b[c]=0
b['Total']=b[['V','E','D']].sum(axis=1);b=b.nlargest(15,'Total').sort_values('Total')
fig,ax=plt.subplots(figsize=(10.5,6.2))
left=None
for ved in ['V','E','D']:
    vals=b[ved]
    ax.barh(b.index,vals,left=left,color=VED_COLORS[ved],label=ved)
    left=vals if left is None else left+vals
ax.set_title('Салбарын corrected ROP exposure — TOP 15',fontweight='bold');ax.set_xlabel('ROP');ax.legend();ax.spines[['top','right']].set_visible(False)
fig.tight_layout();fig.savefig(assets/'branch.png',dpi=160,bbox_inches='tight');plt.close(fig)

# Compose dashboard canvas.
W=1800;H=2300
canvas=Image.new('RGB',(W,H),'#F8FAFC')
draw=ImageDraw.Draw(canvas)
font_paths=['/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf','/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf']
font=ImageFont.truetype(font_paths[0],24);bold=ImageFont.truetype(font_paths[1],32);title=ImageFont.truetype(font_paths[1],42);small=ImageFont.truetype(font_paths[0],18)
# Header
draw.rounded_rectangle((30,25,W-30,135),radius=20,fill='#12263A')
draw.text((60,48),'ROP & Нөөцийн удирдлагын ахисан түвшний дашбоард',font=title,fill='white')
draw.text((62,100),'Python ETL + Power BI-ready model | Snapshot 2026-08-13',font=small,fill='#DDEBF7')
# KPI cards
cards=[('Нийт SKU',f"{meta['sku_count']:,}"),('Inventory Position',f"{meta['on_hand_total_matched']:,.0f}"),('Total ROP',f"{meta['network_rop_total']:,.0f}"),('ROP gap',f"{meta['rop_gap_total']:,.0f}"),('Critical V/E',f"{meta['critical_ve_sku']:,}"),('Mapping rate',f"{meta['mapping']['mapping_rate_rows']:.1%}")]
colors=['#1F4E5F','#2563EB','#0F766E','#F97316','#B91C1C','#7C3AED']
card_w=(W-90)//3;card_h=115
for idx,(label,val) in enumerate(cards):
    r=idx//3;c=idx%3;x=30+c*(card_w+15);y=160+r*(card_h+15)
    draw.rounded_rectangle((x,y,x+card_w,y+card_h),radius=14,fill='white',outline='#D1D5DB',width=2)
    draw.rectangle((x,y,x+card_w,y+8),fill=colors[idx])
    draw.text((x+18,y+20),label,font=small,fill='#6B7280')
    draw.text((x+18,y+48),val,font=bold,fill='#111827')

# Place chart images.
def paste_fit(path,box):
    im=Image.open(path).convert('RGB')
    x1,y1,x2,y2=box;target=(x2-x1,y2-y1)
    im.thumbnail(target,Image.Resampling.LANCZOS)
    px=x1+(target[0]-im.width)//2;py=y1+(target[1]-im.height)//2
    canvas.paste(im,(px,py))

paste_fit(assets/'status.png',(30,440,885,920))
paste_fit(assets/'ved.png',(915,440,1770,920))
paste_fit(assets/'shortage.png',(30,950,1770,1565))
paste_fit(assets/'branch.png',(30,1595,1770,2210))
draw.text((40,2250),'Note: Current inventory is network-level. Branch stock status becomes available when Branch + SKU_ID + OnHand are supplied.',font=small,fill='#4B5563')
canvas.save(BASE/'dashboard_preview.png',quality=92)
print(BASE/'dashboard_preview.png')
