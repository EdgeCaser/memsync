# Orchestrator Build Checklist - 2026-04-15

## Introduction

This checklist provides a granular breakdown of tasks required to implement "Fast Mode" and "Rigor Mode" within the orchestrator, integrating them with the existing 3-state confidence model and routing mechanisms.

## Build Items

### Core Mode Delineation & Routing Engine Modifications

-   [ ] **Define Mode Input Mechanism:**
    -   [ ] Design and implement API parameter/CLI flag for mode selection (Fast/Rigor).
    -   [ ] Establish default mode logic (e.g., Fast Mode by default unless specified).
    -   [ ] Document user override procedures.
-   [ ] **Routing Engine Refactor:**
    -   [ ] Modify routing engine to accept `mode` as a primary input.
    -   [ ] **Fast Mode Routing Logic Implementation:**
        -   [ ] Develop logic to select simpler, direct analytical paths.
        -   [ ] Configure default scenario class for Fast Mode (e.g., `BasicAnalysisScenario`).
        -   [ ] Ensure suppression of automatic debate initiation.
        -   [ ] Ensure suppression of complex panel gates.
    -   [ ] **Rigor Mode Routing Logic Validation:**
        -   [ ] Verify existing logic for thorough scenario class determination.
        -   [ ] Confirm activation of all complex analytical paths and governance policies.
-   [ ] **Governance Policy Adaptation:**
    -   [ ] **Fast Mode Policy Configuration:**
        -   [ ] Adjust `provisional` confidence thresholds for initial output in Fast Mode.
        -   [ ] Implement internal flagging for `more_rigor_recommended` and `not_ready` without user-facing auto-escalation.
    -   [ ] **Rigor Mode Policy Validation:**
        -   [ ] Confirm existing strict confidence thresholds are fully active.
        -   [ ] Verify `more_rigor_recommended` auto-initiates internal analysis (e.g., debate).
        -   [ ] Ensure `not_ready` triggers uncertainty payload generation.

### Agent Behavior & Output Customization

-   [ ] **Fast Mode Agent/Orchestrator Behavior Adjustments:**
    -   [ ] Implement mechanisms for single, clean analysis delivery by agents.
    -   [ ] Remove/Disable complex scenario class defaults for Fast Mode runs.
    -   [ ] Suppress generation of "more rigor" or "debate" prompts to the user.
    -   [ ] Bypass user-facing confirmation steps (panel gates).
    -   [ ] Adapt/Develop agents to produce structured outputs over multi-turn debates.
-   [ ] **Rigor Mode Agent/Orchestrator Behavior Confirmation:**
    -   [ ] Confirm active initiation and management of debate formats.
    -   [ ] Verify full application of all advanced features, substates, and governance policies.
    -   [ ] Ensure confidence thresholds effectively gate progress and trigger escalations.
    -   [ ] Implement robust generation of the "uncertainty payload" for unresolved cases.

### User Experience & Escalation Paths

-   [ ] **Fast Mode Confidence Presentation:**
    -   [ ] Design and implement subtle confidence indicators (e.g., small icon, tooltip).
    -   [ ] Review messaging to avoid alarming users unless critically low confidence.
-   [ ] **Explicit Escalation Mechanism:**
    -   [ ] Design user flow for escalating from Fast Mode to Rigor Mode (e.g., "Rerun with Rigor" button/command).
    -   [ ] Implement context transfer from Fast Mode run to Rigor Mode.
-   [ ] **Output Differentiation:**
    -   [ ] Ensure clear visual/structural distinction between Fast Mode and Rigor Mode results.

### Testing & Validation

-   [ ] **Unit Tests:**
    -   [ ] Develop unit tests for routing engine modifications.
    -   [ ] Develop unit tests for governance policy adaptations.
    -   [ ] Develop unit tests for agent behavior changes (both modes).
-   [ ] **Integration Tests:**
    -   [ ] Create end-to-end integration tests for Fast Mode.
    -   [ ] Create end-to-end integration tests for Rigor Mode.
    -   [ ] Create integration tests for escalation paths (Fast to Rigor).
-   [ ] **Performance Benchmarking:**
    -   [ ] Benchmark Fast Mode to ensure performance objectives are met.
-   [ ] **Confidence Model Validation:**
    -   [ ] Validate 3-state model behavior in Fast Mode (especially `provisional` output).
    -   [ ] Validate 3-state model behavior in Rigor Mode (escalations, uncertainty payload).

### Documentation

-   [ ] Update `orchestrator-decision-spec-2026-04-15.md` (already done).
-   [ ] Update `orchestrator-implementation-plan-2026-04-15.md` (already done).
-   [ ] Update `orchestrator-build-checklist-2026-04-15.md` (this document, completed).
-   [ ] Update relevant user-facing documentation (e.g., `getting-started.md`, `global-memory-guide.md` if applicable) to reflect Fast/Rigor Modes.
-   [ ] Update developer documentation regarding new API parameters/CLI flags.