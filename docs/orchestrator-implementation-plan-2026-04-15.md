# Orchestrator Implementation Plan - 2026-04-15

## Introduction

This document details the implementation plan for introducing "Fast Mode" and "Rigor Mode" into the orchestrator design, building upon the existing 3-state confidence model (provisional, more_rigor_recommended, not_ready). The primary goal is to provide flexible orchestration tailored to different user needs, from quick analysis to comprehensive, debated outcomes.

## Phase 1: Core Mode Delineation and Routing Engine Modifications

**Objective:** Establish the fundamental distinction between Fast Mode and Rigor Mode and modify the routing engine to correctly direct requests based on the selected mode.

1.  **Define Mode Input:**
    *   **Task:** Introduce a clear input mechanism (e.g., a parameter in API calls, CLI flag) to specify whether an orchestration request should run in "Fast Mode" or "Rigor Mode."
    *   **Considerations:** Default mode, user override options.
2.  **Routing Engine Refactor ("Seam"):**
    *   **Task:** Modify the orchestrator's routing engine to accept the mode as a primary input. This input will influence the selection of the "scenario class" and subsequent execution path.
    *   **Fast Mode Routing Logic:**
        *   Implement logic to prioritize simpler, direct analytical paths.
        *   Default to a general-purpose scenario class or a lightweight, mode-specific equivalent.
        *   Ensure suppression of automatic debate initiation and complex panel gates.
    *   **Rigor Mode Routing Logic:**
        *   Maintain existing logic for thorough scenario class determination.
        *   Ensure activation of all complex analytical paths and governance policies.
3.  **Governance Policy Adaptation:**
    *   **Task:** Review and adapt existing governance policies to explicitly consider the operating mode.
    *   **Fast Mode Policy:**
        *   Confidence thresholds for `provisional` output will be more permissive for initial output presentation.
        *   Internal flagging for `more_rigor_recommended` or `not_ready` will occur, but these states will not trigger automatic user-facing escalations.
    *   **Rigor Mode Policy:**
        *   Existing strict confidence thresholds and escalation triggers remain fully active.
        *   `more_rigor_recommended` will automatically initiate further internal analysis (e.g., debate).
        *   `not_ready` will trigger the generation of an uncertainty payload.

## Phase 2: Agent Behavior and Output Customization

**Objective:** Adjust internal agent behavior and orchestrator output formats to align with the characteristics of each mode.

1.  **Fast Mode Agent/Orchestrator Behavior:**
    *   **Task:** Implement mechanisms to ensure agents operating in Fast Mode deliver single, clean analyses.
    *   **No Scenario Class Defaults (Fast Mode):** Ensure the orchestrator does not impose complex scenario class defaults for Fast Mode runs.
    *   **Suppress Escalation Prompts:** Modify orchestrator logic to prevent the generation of "more rigor" or "debate" prompts to the user.
    *   **Disable Panel Confirmation Gates:** Bypass any user-facing confirmation steps inherent to Rigor Mode.
    *   **Structured Analysis Preference:** Develop or adapt agents to produce a single, structured output rather than initiating multi-turn debates.
2.  **Rigor Mode Agent/Orchestrator Behavior:**
    *   **Task:** Confirm and reinforce existing agent behaviors for Rigor Mode.
    *   **Default Debate Format:** Ensure the orchestrator actively initiates and manages debate formats among agents.
    *   **Full Orchestrator Spec Application:** Verify that all advanced features, substates, and governance policies are active and correctly applied.
    *   **Confidence Gating:** Ensure confidence thresholds effectively gate progress and trigger appropriate escalations.
    *   **Uncertainty Payload Generation:** Implement robust mechanisms for generating the "uncertainty payload" when a definitive resolution cannot be reached in Rigor Mode.

## Phase 3: User Experience and Escalation Paths

**Objective:** Design and implement the user-facing aspects, including how users request escalation from Fast Mode to Rigor Mode.

1.  **Confidence Presentation (Fast Mode):**
    *   **Task:** Implement subtle ways to display confidence (e.g., a small indicator, a hover-over tooltip) without being alarming.
    *   **Considerations:** Avoid language that suggests incompleteness unless the confidence is critically low.
2.  **Explicit Escalation Mechanism:**
    *   **Task:** Develop a clear and intuitive way for users to "escalate" a Fast Mode output to Rigor Mode (e.g., a "Get More Detail," "Rerun with Rigor," or "Debate This" button/command).
    *   **Flow:** Define the transition flow, including carrying over relevant context from the Fast Mode run.
3.  **Output Differentiation:**
    *   **Task:** Ensure the output format and content clearly distinguish between Fast Mode and Rigor Mode results, highlighting the level of analysis performed.

## Testing and Validation

*   **Unit Tests:** Develop unit tests for the routing engine modifications, governance policy adaptations, and agent behavior changes for both modes.
*   **Integration Tests:** Create integration tests to verify the end-to-end flow of both Fast Mode and Rigor Mode, including escalation paths.
*   **Performance Benchmarking:** Benchmark Fast Mode to ensure it meets its performance objectives for quick analysis.
*   **Confidence Model Validation:** Validate that the 3-state model behaves as expected in both modes, particularly for `provisional` outputs in Fast Mode and active escalations in Rigor Mode.

## Future Considerations

*   Dynamic Mode Switching: Explore the possibility of the orchestrator dynamically recommending a switch to Rigor Mode even from an explicit Fast Mode request if internal metrics indicate severe issues.
*   Hybrid Mode: Investigate a "hybrid" approach where certain aspects of rigor are selectively applied within a predominantly fast context.