// Create a Text parameter named pDataFolder, for example:
// C:\ROP_Advanced_Dashboard\powerbi_data\

// Query: DimSKU
let
    Source = Csv.Document(File.Contents(pDataFolder & "dim_sku.csv"), [Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.Csv]),
    Headers = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),
    Types = Table.TransformColumnTypes(Headers, {
        {"SKU_ID", Int64.Type}, {"SKU_Name", type text}, {"MasterKey", type text},
        {"Category", type text}, {"VED", type text}, {"LeadTime_Months", type number},
        {"LeadTime_Status", type text}, {"Sales_7M", type number}, {"AvgMonthlyDemand", type number},
        {"ROP_Current", type number}, {"ROP_Corrected", type number}, {"ROP_Used", type number},
        {"ROP_Source", type text}, {"ROP_Confidence", type text}, {"ActiveBranchCount", Int64.Type},
        {"HasROP", type logical}, {"HasDetailROP", type logical}
    })
in
    Types

// Query: DimBranch
let
    Source = Csv.Document(File.Contents(pDataFolder & "dim_branch.csv"), [Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.Csv]),
    Headers = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),
    Types = Table.TransformColumnTypes(Headers, {{"BranchName", type text}, {"Branch_ID", type text}, {"BranchGroup", type text}, {"IsNetwork", type logical}})
in
    Types

// Query: FactSKUStatus
let
    Source = Csv.Document(File.Contents(pDataFolder & "fact_sku_status.csv"), [Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.Csv]),
    Headers = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),
    Types = Table.TransformColumnTypes(Headers, {
        {"SKU_ID", Int64.Type}, {"Branch_ID", type text}, {"BranchName", type text},
        {"OnHand", type number}, {"OnOrder", type number}, {"Backorder", type number},
        {"InventoryPosition", type number}, {"ROP", type number}, {"ROPGap", type number},
        {"CoverageRatio", type number}, {"Status", type text}, {"WeightedGap", type number},
        {"PriorityScore", type number}, {"SnapshotDate", type date}, {"Scope", type text},
        {"IsCritical", type logical}, {"HasROP", type logical}, {"HasInventory", type logical}
    })
in
    Types

// Query: FactInventorySnapshot
let
    Source = Csv.Document(File.Contents(pDataFolder & "fact_inventory_snapshot.csv"), [Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.Csv]),
    Headers = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),
    Types = Table.TransformColumnTypes(Headers, {
        {"InventoryLineID", Int64.Type}, {"SKU_ID", Int64.Type}, {"Branch_ID", type text},
        {"InventoryName", type text}, {"Manufacturer", type text}, {"Supplier", type text},
        {"OnHand", type number}, {"OnOrder", type number}, {"Backorder", type number},
        {"InventoryPosition", type number}, {"SnapshotDate", type date}, {"MatchStatus", type text},
        {"Scope", type text}, {"SourceFile", type text}
    })
in
    Types

// Query: FactBranchROP
let
    Source = Csv.Document(File.Contents(pDataFolder & "fact_branch_rop.csv"), [Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.Csv]),
    Headers = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),
    Types = Table.TransformColumnTypes(Headers, {
        {"SKU_ID", Int64.Type}, {"Branch_ID", type text}, {"BranchName", type text},
        {"ROP", type number}, {"AvgMonthlyDemand_Branch", type number}, {"DemandSD", type number},
        {"LeadTimeMean", type number}, {"LeadTimeSD", type number}, {"Sales7M", type number},
        {"ABC", type text}, {"XYZ", type text}, {"ABC_XYZ", type text},
        {"Z_VED", type number}, {"Z_ABCXYZ", type number}, {"Z_Final", type number}
    })
in
    Types

// Import dim_date.csv, dim_status.csv, dim_ved.csv and mapping_review.csv with the same pattern.
