from pathlib import Path
import json
import pandas as pd
import numpy as np

BASE=Path(__file__).resolve().parent
D=BASE/'data'
sku=pd.read_csv(D/'dim_sku.csv.gz',low_memory=False)
branch=pd.read_csv(D/'dim_branch.csv.gz',low_memory=False)
rop=pd.read_csv(D/'fact_branch_rop.csv.gz',low_memory=False)
inv=pd.read_csv(D/'fact_inventory_snapshot.csv.gz',low_memory=False)
status=pd.read_csv(D/'fact_sku_status.csv.gz',low_memory=False)
alias=pd.read_csv(D/'alias_mapping.csv.gz',low_memory=False)
meta=json.loads((D/'metadata.json').read_text(encoding='utf-8'))

checks={}
checks['dim_sku_rows']=len(sku)
checks['dim_sku_unique_id']=sku['SKU_ID'].is_unique
checks['dim_branch_rows']=len(branch)
checks['dim_branch_unique_id']=branch['Branch_ID'].is_unique
checks['fact_branch_rop_rows']=len(rop)
checks['fact_branch_rop_unique_key']=bool(~rop.duplicated(['SKU_ID','Branch_ID']).any())
checks['fact_branch_rop_sku_fk']=set(rop['SKU_ID']).issubset(set(sku['SKU_ID']))
checks['fact_branch_rop_branch_fk']=set(rop['Branch_ID']).issubset(set(branch['Branch_ID']))
checks['status_rows']=len(status)
checks['status_unique_key']=bool(~status.duplicated(['SKU_ID','Branch_ID','SnapshotDate']).any())
checks['status_sku_fk']=set(status['SKU_ID']).issubset(set(sku['SKU_ID']))
checks['status_counts_sum']=sum(meta['status_counts'].values())
checks['network_rop_sum_match']=bool(np.isclose(status['ROP'].sum(),sku['ROP_Used'].sum()))
checks['corrected_branch_rop_sum']=float(rop['ROP'].sum())
checks['negative_rop_rows']=int((rop['ROP']<0).sum())
checks['negative_gap_rows']=int((status['ROPGap'].dropna()<0).sum())
checks['coverage_invalid_rows']=int(((status['CoverageRatio'].dropna()<0)&(status['InventoryPosition']>=0)).sum())
checks['mapping_matched_rows']=int(inv['SKU_ID'].notna().sum())
checks['mapping_unmatched_rows']=int(inv['SKU_ID'].isna().sum())
checks['alias_unique_key']=alias['NormalizedKey'].is_unique
checks['all_ok']=all([checks['dim_sku_unique_id'],checks['dim_branch_unique_id'],checks['fact_branch_rop_unique_key'],checks['fact_branch_rop_sku_fk'],checks['fact_branch_rop_branch_fk'],checks['status_unique_key'],checks['status_sku_fk'],checks['network_rop_sum_match'],checks['negative_rop_rows']==0,checks['negative_gap_rows']==0,checks['coverage_invalid_rows']==0,checks['alias_unique_key']])

(BASE/'QA_REPORT.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
print(json.dumps(checks,ensure_ascii=False,indent=2,default=str))
