# APAgent SKU Price QA

This report is generated from `APAgent/data/apagent.db` by `APAgent/scripts/sku_price_qa.py`.

## Summary

- Total price records: `2562`
- Latest run: `58`
- Latest run records: `8`
- Latest run priced OK: `8`
- Latest run errors/no-price: `0`
- Duplicate semantic keys across all runs: `671`
- Duplicate extra rows across all runs: `911`
- Canonical records after de-duplication: `1651`
- Latest run bad iPhone size-model rows: `0`

## Promotion States

| State | Count | Meaning |
|---|---:|---|
| selection_path_required | 868 | Has price and selection path; ordering must replay page selections or extract part number. |
| direct_part_number | 381 | Can be promoted to `skus` with a part number. |
| blocked_bad_model | 202 | Data is priced but model normalization is wrong. |
| needs_order_path | 125 | Has price but lacks part number and selection path. |
| not_promotable | 75 | Missing successful price. |

## Latest Run By Family

| Family | Total | Priced OK | Error/No Price |
|---|---:|---:|---:|
| Apple Watch | 8 | 8 | 0 |

## Top Latest Run Errors

| Count | Family | Error |
|---:|---|---|

## Selection Strategy Coverage

| Strategy | Count |
|---|---:|
| page_click | 1051 |
| fixed_product_page | 933 |
| url_path | 472 |
| <empty> | 106 |

## Capture Cache

| Kind | Entries | Hits |
|---|---:|---:|
| observed_options | 7 | 14 |
| prefix_options | 547 | 327 |
| selection_path | 401 | 687 |

## Recommended Next Capture Order

1. Re-run iPhone configurable capture after model normalization is fixed.
2. Promote fixed-product records with `part_number` directly into a review queue.
3. For configurable Mac/iPad/Watch, enumerate only currently enabled options after each selection.
4. Stop treating unavailable combinations as failed SKUs; record them as unavailable option edges.
5. Validate one representative SKU per family through Add to Bag before expanding the matrix.
