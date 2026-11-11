# Native freight workflow (Odoo 19)

- Waybill freight is a native Purchase Order service line on the originating PO.
- `MS-FREIGHT-SERVICE` is a Service with Is a Landed Cost enabled. The default split is by quantity: MegaSanj receipt quantities are kilograms.
- Native Purchase bill creation/matching, vendor bills, payments and refunds remain the accounting workflow. The PO vendor is the creditor; a driver contact is not automatically classified as a vendor.
- When the receipt posts, native Landed Costs are created, computed and validated automatically for positive company-paid freight, using exactly the waybill amount. Contractor-paid or zero freight is excluded. Native AVCO/FIFO and journal configuration are required.
- One native landed cost per waybill prevents duplicate capitalization on retries. Later purchase bills link to the existing cost; Create Landed Costs opens it rather than charging it again. Vendor refunds retain native negative-cost functionality.
- No separate MegaSanj freight receipt or approval is created. Legacy records are retained read-only for audit; the old views/approval are disabled.
- Migration reuses existing draft bills and preserves their amounts. Posted historical bills and stock quantities are not rewritten. Loads already covered by historical posted bills do not get a duplicate payable PO line.
- Physical returns preserve the native service line and financial obligations; finance handles settlement with native bills/refunds.

## PDF reports

Weighbridge and discrepancy PDFs use native Odoo QWeb reports and the standard PDF renderer. There is no custom report subprocess or additional Python dependency. The templates include the logo, shipment/driver details, weights, received composition, timestamps, signatures and the complete dynamic minutes statement. The API and report share the same statement method; unknown data retains blanks and a known zero decision prints zero.
