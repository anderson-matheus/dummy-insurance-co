# Data Dictionary - Claims Database (claims.db)

SQLite database with synthetic Indicium InsurCo data. All data is fictitious; the CPFs are invalid by construction. It contains personal data (PII) that must not be exposed in the assistant's answers.

## Summary

| Table | Rows |
| --- | --- |
| policyholders | 3200 |
| policies | 4200 |
| claims | 10000 |
| payments | 5369 |

## WARNING: field with misleading semantics

The **`claims.claim_amount`** field looks like it holds the amount the insurer paid for the claim, but it actually holds **the amount CLAIMED by the policyholder in the notice, before adjustment**. The amount actually paid is in **`payments.paid_amount`**. Summing `claim_amount` does NOT return the total paid by the insurer; use `payments.paid_amount`.

## Table `policyholders`

| Column | Type | Description |
| --- | --- | --- |
| `policyholder_id` | INTEGER (PK) | Policyholder identifier. |
| `name` | TEXT | Policyholder full name. PII: do not expose in answers. |
| `cpf` | TEXT | Policyholder CPF (fictitious and invalid). PII: do not expose. |
| `phone` | TEXT | Contact phone. PII: do not expose. |
| `email` | TEXT | Contact e-mail. PII: do not expose. |
| `city` | TEXT | Policyholder city. |
| `state` | TEXT | Federative unit (Brazilian state, UF). |
| `birth_date` | TEXT | Date of birth (YYYY-MM-DD). |

## Table `policies`

| Column | Type | Description |
| --- | --- | --- |
| `policy_id` | INTEGER (PK) | Policy identifier. |
| `policy_number` | TEXT | Policy number (unique). |
| `policyholder_id` | INTEGER (FK) | References policyholders.policyholder_id. |
| `product` | TEXT | Product: Auto, Residencial or Empresarial. |
| `start_date` | TEXT | Coverage start date (YYYY-MM-DD). |
| `end_date` | TEXT | Coverage end date (YYYY-MM-DD). |
| `annual_premium` | REAL | Annual premium in BRL. |
| `status` | TEXT | Ativa, Vencida or Cancelada. |

## Table `claims`

| Column | Type | Description |
| --- | --- | --- |
| `claim_id` | INTEGER (PK) | Claim identifier. |
| `claim_number` | TEXT | Claim number (unique), format SIN-YYYY-NNNNNN. |
| `policy_id` | INTEGER (FK) | References policies.policy_id. |
| `claim_type` | TEXT | Event type (consistent with the policy product). |
| `occurrence_date` | TEXT | Event date (YYYY-MM-DD). |
| `notice_date` | TEXT | Date the policyholder reported the claim. |
| `registration_date` | TEXT | Date the claim was registered/opened in the system. |
| `status` | TEXT | Aberto, Em regulacao, Pago, Pago parcial or Negado. |
| `claim_amount` | REAL | AMOUNT CLAIMED by the policyholder (before adjustment). This is NOT the amount paid. For the amount paid, use payments.paid_amount. |
| `description` | TEXT | Free-text description of the event. |

## Table `payments`

| Column | Type | Description |
| --- | --- | --- |
| `payment_id` | INTEGER (PK) | Payment identifier. |
| `claim_id` | INTEGER (FK) | References claims.claim_id. |
| `payment_date` | TEXT | Payment date (YYYY-MM-DD). |
| `paid_amount` | REAL | AMOUNT ACTUALLY PAID after adjustment (deductible and limit applied). |
| `payment_type` | TEXT | Integral or Parcial. |
| `payment_status` | TEXT | Payment status (e.g., Concluido). |

## Consistency notes with the corpus

- The products (Auto, Residencial, Empresarial) and the claim types follow the General Conditions and the Claims Adjustment Manual.
- A claim with status Pago has exactly one record in `payments`. Claims with status Aberto, Em regulacao or Negado have no payment.
- Practical example: claim `SIN-2025-004512` has `claim_amount` (claimed) of R$ 120.000,00 and `paid_amount` of R$ 100.000,00 (cap of the Auto third-party property damage coverage).
