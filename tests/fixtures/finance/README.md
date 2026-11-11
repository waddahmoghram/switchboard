Synthetic test data for the finance connector. Every institution, account number, and merchant is
fictional; the account numbers are not valid card or bank numbers.

- example_cu_checking.ofx: OFX 1.0.2 SGML (unclosed data elements), CHARSET:1252, a checking account
  ending in 6789, four transactions, ledger and available balances.
- example_card.qfx: OFX 2.2 XML with Quicken's INTU.BID element, a credit card ending in 4321, a purchase
  with an escaped ampersand, and a payment.
- example_cu_savings.csv: a CSV export with two identical rows on the same day (two real transfers).
