# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Primary user is a paper-trading operator and quant lead who needs to scan BTCQuant/TANDEM's current state, performance, risk, and operational trust quickly. This is inferred from the redesign brief because no interactive product interview was available in this unattended run.

## Product Purpose

Read-only dashboard for understanding what the PAPER system is doing now, whether the displayed state is trustworthy, and what risk or operational issue needs attention. It must never imply live authorization or fabricate unavailable metrics.

## Operating Context

Used as an always-on monitoring cockpit across desktop and mobile breakpoints. The dashboard consumes existing read-only reporting/API surfaces and must preserve authentication, fail-closed status semantics, and PAPER runtime isolation.

## Capabilities and Constraints

Three views—Monitor, Performance, and Risk—present Trend, Carry, market context, equity, events, protection, health, qualification, and freshness. PAPER, qualification, testnet authorization, synthetic/proxy Carry, stale, unknown, unavailable, realized, modeled, and backtest states must remain distinct. No trading, exchange calls, secrets, wallets, schema changes, or financial mutations are in scope.

## Evidence on Hand

Existing dashboard implementation and read-only API/reporting surfaces in `dashboard/`, plus prior visual review artifacts. No new claims, data, or external brand assets may be invented.

## Product Principles

- Trust state is visible before interpretation.
- Critical risk and fail-closed states outrank vanity metrics.
- Read-only facts keep their provenance and uncertainty.
- Dense information remains scannable on a phone.
- Visual polish supports operational judgment rather than decoration.

## Accessibility & Inclusion

Keyboard navigation, visible focus, semantic status text, contrast, reduced motion, responsive layouts, and non-color-only critical states are required. This is inferred from the explicit acceptance criteria in the redesign brief.
