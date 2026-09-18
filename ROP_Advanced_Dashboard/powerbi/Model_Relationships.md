# Power BI semantic model

Rename the imported CSV tables as follows:

| CSV file | Power BI table |
|---|---|
| `dim_sku.csv` | `DimSKU` |
| `dim_branch.csv` | `DimBranch` |
| `dim_date.csv` | `DimDate` |
| `dim_status.csv` | `DimStatus` |
| `dim_ved.csv` | `DimVED` |
| `fact_branch_rop.csv` | `FactBranchROP` |
| `fact_inventory_snapshot.csv` | `FactInventorySnapshot` |
| `fact_sku_status.csv` | `FactSKUStatus` |
| `mapping_review.csv` | `MappingReview` |

## Relationships

Create single-direction, one-to-many relationships:

1. `DimSKU[SKU_ID]` 1 → * `FactSKUStatus[SKU_ID]`
2. `DimSKU[SKU_ID]` 1 → * `FactInventorySnapshot[SKU_ID]`
3. `DimSKU[SKU_ID]` 1 → * `FactBranchROP[SKU_ID]`
4. `DimBranch[Branch_ID]` 1 → * `FactSKUStatus[Branch_ID]`
5. `DimBranch[Branch_ID]` 1 → * `FactInventorySnapshot[Branch_ID]`
6. `DimBranch[Branch_ID]` 1 → * `FactBranchROP[Branch_ID]`
7. `DimDate[Date]` 1 → * `FactSKUStatus[SnapshotDate]`
8. `DimDate[Date]` 1 → * `FactInventorySnapshot[SnapshotDate]`
9. `DimStatus[Status]` 1 → * `FactSKUStatus[Status]`
10. `DimVED[VED]` 1 → * `DimSKU[VED]`

Do not create a direct relationship between `FactSKUStatus` and `FactBranchROP`.

## Model notes

- `FactSKUStatus` is the current actionable snapshot and should drive executive KPIs.
- `FactBranchROP` is a corrected branch-level ROP exposure fact. Until branch-level inventory is supplied, it should not be interpreted as a branch stock status.
- `FactInventorySnapshot` retains the imported inventory lines and mapping status for audit.
- `DimSKU[ROP_Confidence] = "Бага"` means the network ROP uses a fallback value rather than a corrected detailed calculation.
