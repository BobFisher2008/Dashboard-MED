# ROP ба үлдэгдлийн ахисан түвшний дашбоард

Энэ багц нь нэг цэвэр star-schema дата загварыг **Python интерактив дашбоард** болон **Power BI**-д зэрэг ашиглахаар зохион байгуулсан.

## 1. Багцын бүрэлдэхүүн

- `app.py` — Bokeh server дээр ажиллах интерактив Python дашбоард.
- `refresh_inventory.py` — шинэ XLSX/XLSB/CSV үлдэгдлийг уншиж дата модель шинэчлэх ETL.
- `build_model.py` — ROP master, branch ROP, alias mapping болон эхний үлдэгдлээс бүх star schema-г дахин үүсгэнэ.
- `common.py` — нэр нормалчлах, файл таних, mapping, status тооцох нийтлэг функц.
- `data/` — Python app-ийн шахсан CSV модель.
- `powerbi_data/` — Power BI-д шууд импортлох UTF-8 CSV хүснэгтүүд.
- `powerbi/` — DAX measure, Power Query M, theme, relationship болон report layout.
- `Inventory_Upload_Template.xlsx` — ирээдүйн үлдэгдлийн стандарт оролтын загвар.
- `dashboard_preview.html` — эхний snapshot-ийн standalone интерактив preview.

## 2. Эхний snapshot-ийн хамрах хүрээ

- 6,781 SKU
- 89 салбарын corrected ROP detail
- 199,749 SKU–салбарын ROP мөр
- Сүлжээний working ROP: 459,105 нэгж
- Corrected branch ROP: 356,296 нэгж
- 145,625 үлдэгдлийн мөрөөс 136,144 мөр exact SKU/alias mapping-тай
- Мөрийн mapping rate: 93.5%
- Тоо хэмжээний mapping rate: 95.5%
- Одоогийн нийт ROP gap: 39,008.9 нэгж

## 3. Python дашбоард ажиллуулах

### Windows

1. Python 3.11+ суулгана.
2. Төслийн хавтсанд Command Prompt нээнэ.
3. Сангуудыг суулгана:

```bash
pip install -r requirements.txt
```

4. `run_dashboard.bat` ажиллуулна.

Эсвэл:

```bash
python -m bokeh serve --show app.py
```

> **Windows terminal анхааруулга:** Bokeh 3.x дээр `server` гэсэн subcommand
> байхгүй. `python -m bokeh server --show app.py` гэж ажиллуулахад
> `invalid choice: 'server'` алдаа гарна. Заавал `serve` гэж бичнэ:
>
> ```powershell
> cd "C:\Users\GAWA\Downloads\ROP_Advanced_Dashboard_Package (1)\ROP_Advanced_Dashboard"
> python -m bokeh serve --show app.py
> ```
>
> Эсвэл энэ хавтас дахь `run_dashboard.bat` файлыг ажиллуулна.

Дашбоард browser дээр нээгдэнэ.

### Үндсэн боломжууд

- XLSX, XLSB, CSV үлдэгдлийн файл upload хийх.
- SKU ID байгаа бол шууд холбох; байхгүй бол alias mapping ашиглах.
- Сүлжээний болон салбарын файлыг автоматаар ялгах.
- On Hand, On Order, Backorder-оос Inventory Position гаргах.
- ROP gap, coverage, status, VED critical risk шинэчлэх.
- VED, status, category, branch, confidence, нэрээр шүүх.
- TOP shortage, ROP-vs-stock scatter, branch ROP exposure, data-quality review.
- Шүүсэн хүснэгтийг CSV болгон татах.
- Upload snapshot-уудыг `snapshots/` хавтсанд хадгалах.
- "Шүүлтүүр цэвэрлэх" болон "Зөвхөн эрсдэлтэй" товчлуур.
- Хүснэгтийн баганыг дарахад эрэмбэлэх (sort).
- Хүснэгтээс SKU сонгоход дэлгэрэнгүй мэдээлэл харах (drill-through).
- Mapping шалгалтын хүснэгтээс санал зөвшөөрөх/татгалзах, alias-д хадгалах.
- Дээд талд өгөгдлийн шинэчлэлийн мэдээлэл (сүүлийн огноо, эх файл, mapping rate).

## 4. Командын мөрөөр refresh хийх

```bash
python refresh_inventory.py \
  --inventory "C:\Data\Inventory_2026-08-20.xlsx" \
  --snapshot-date 2026-08-20
```

Нэмэлт сонголт:

```bash
--excess-threshold 2.0
--missing-as-zero
```

`--missing-as-zero`-г зөвхөн тухайн экспорт идэвхтэй бүх SKU-г бүрэн агуулдаг нь баталгаатай үед хэрэглэнэ.

Refresh хийсний дараа:

- `data/fact_inventory_snapshot.csv.gz`
- `data/fact_sku_status.csv.gz`
- `powerbi_data/fact_inventory_snapshot.csv`
- `powerbi_data/fact_sku_status.csv`
- `mapping_review.csv`

шинэчлэгдэнэ.

## 5. Power BI угсрах

1. Power BI Desktop нээнэ.
2. `powerbi_data/` хавтас дахь CSV-үүдийг импортлоно.
3. Хүснэгтүүдийг `powerbi/Model_Relationships.md`-ийн дагуу нэрлэж холбоно.
4. `powerbi/DAX_Measures.dax` дахь measure-үүдийг Measures хүснэгтэд үүсгэнэ.
5. `powerbi/theme.json` theme-г импортлоно.
6. `powerbi/Report_Layout_Spec.md`-ийн 5 page layout-ыг ашиглана.
7. Refresh автоматжуулахдаа `schedule_refresh.ps1`-ийг Windows Task Scheduler-д ажиллуулж, дараа нь Power BI dataset refresh хийнэ.

Энэ орчинд Power BI Desktop байхгүй тул `.pbix` binary файлыг энд үүсгээгүй. Харин Power BI-д импортлох star schema, M query, DAX measure, theme болон report specification бүрэн багтсан.

## 6. ROP томьёо

Corrected ROP:

```text
CEILING(
    AvgDemand × MeanLeadTime
    + MAX(Z_VED, Z_ABCXYZ)
      × SQRT(
          MeanLeadTime × DemandSD²
          + AvgDemand² × LeadTimeSD²
        ),
    1
)
```

Inventory Position:

```text
OnHand + OnOrder - Backorder
```

Status:

- Inventory Position ≤ 0 ба ROP > 0 → `Тасарсан`
- Inventory Position < ROP → `ROP-оос доош`
- Inventory Position ≥ ROP × excess threshold → `Илүүдэл`
- Бусад → `Хэвийн`
- Үлдэгдлийн мэдээлэлгүй → `Өгөгдөл алга`
- ROP ≤ 0 → `ROP байхгүй`

## 7. Өгөгдлийн чанарын чухал нөхцөл

Ирээдүйн автоматжуулалтад дараах талбарыг ERP export-д тогтмол оруулах нь хамгийн чухал:

- SnapshotDate
- Branch code/name
- SKU_ID
- ProductName
- OnHand
- OnOrder
- Backorder
- Supplier
- Manufacturer

SKU_ID ба branch code тогтмол байвал fuzzy name matching бараг шаардлагагүй болно.
