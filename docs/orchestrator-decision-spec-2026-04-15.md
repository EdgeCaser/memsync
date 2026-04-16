# Orchestrator Decision Specification - 2026-04-15

## Introduction

This document outlines the decision-making process and specification for the orchestrator, incorporating a new dual-mode operation: "Fast Mode" and "Rigor Mode." This design aims to provide flexibility based on the user's need for speed versus thoroughness, while integrating with the existing 3-state confidence model (provisional, more_rigor_recommended, not_ready).

## Existing 3-State Confidence Model Recap

*   **Provisional:** The orchestrator has generated a plausible initial output with reasonable confidence. This state often implies an opportunity for further refinement or escalation if needed.
*   **More Rigor Recommended:** The orchestrator has identified ambiguities, lower confidence levels, or potential edge cases that warrant deeper analysis. Escalation to a more rigorous process is recommended.
*   **Not Ready:** The orchestrator cannot produce a satisfactory or safe output given the current context or constraints. Further intervention or re-evaluation is required.

These states are determined by internal governance policies and confidence metrics, replacing earlier numeric thresholds.

## Dual-Mode Orchestration: Fast Mode vs. Rigor Mode

To cater to diverse user needs, the orchestrator will operate in two distinct modes:

### Fast Mode

Fast Mode is designed for scenarios where quick, concise output is prioritized, and detailed scrutiny is secondary unless explicitly requested.

**Characteristics:**
*   **Single Analysis, Clean Output:** The orchestrator will perform a single, efficient analysis to generate a straightforward result. Output will be clean and direct, avoiding extraneous details or prompts for escalation by default.
*   **Confidence Visible but Not Alarming:** Internal confidence metrics will be maintained but presented subtly, without immediately triggering escalation prompts or "warning" signals to the user. The `provisional` state will often be the default output state in Fast Mode.
*   **Escalation Available on Demand:** Users can explicitly request further rigor or detail if they deem the Fast Mode output insufficient. This escalation will transition the operation to Rigor Mode.
*   **Minimal Orchestrator Machinery:**
    *   No scenario class defaults: The orchestrator will not assume a complex scenario requiring specific classes or intricate logic.
    *   No automatic escalation prompts: The system will not proactively suggest "more rigor" or "debate" options.
    *   No panel confirmation gates: Direct path to output without intermediate user confirmations or validation steps.
*   **Structured Analysis over Debate (Optional):** The primary mechanism might be a single structured analysis rather than a multi-turn debate format. While a debate format *could* be initiated if the user escalates, it's not the default.

**Integration with 3-State Model (Fast Mode):**
In Fast Mode, an output will primarily be considered `provisional`. If the internal confidence is particularly low, it might still register as `more_rigor_recommended` or `not_ready`, but the system will *not* automatically expose this or prompt for escalation. The responsibility to request further rigor lies with the user.

### Rigor Mode

Rigor Mode is designed for scenarios demanding comprehensive, multi-faceted analysis, leveraging the full capabilities of the orchestrator, including debate mechanisms and strict governance policies.

**Characteristics:**
*   **Debate Format by Default:** Operations in Rigor Mode will, by default, employ a debate format among internal agents or analysis paths to thoroughly explore the problem space.
*   **Full Orchestrator Spec Applies:** All advanced features, substates, and governance policies of the orchestrator design are fully active.
*   **Scenario Class Determines Starting Point:** The specific "scenario class" of the task will dictate the initial setup, required agents, and analytical pathways. This is a critical input to the routing engine.
*   **Confidence Thresholds Gate Escalation:** Explicit confidence thresholds (as per governance policies) will actively gate progress. If confidence falls below a certain level, the orchestrator will automatically trigger further analysis, internal debates, or prompt the user for direction.
*   **Uncertainty Payload is Primary Output (Unresolved Cases):** If a definitive answer cannot be reached with sufficient confidence, the primary output will be an "uncertainty payload," detailing the conflicting viewpoints, remaining ambiguities, and proposed next steps for resolution.

**Integration with 3-State Model (Rigor Mode):**
Rigor Mode fully utilizes the 3-state model. `Provisional`, `more_rigor_recommended`, and `not_ready` states will be actively managed and communicated. A `more_rigor_recommended` state will typically lead to automatic internal escalation (e.g., initiating a debate), while `not_ready` will result in a clear indication to the user of the impasse and the uncertainty payload.

## Routing Engine and the Seam to Refactor

The "seam to refactor" for enabling these two modes is primarily the input to the routing engine that asks "what scenario class is this?". This input will now explicitly consider whether the operation is in "Fast Mode" or "Rigor Mode."

*   **Fast Mode Routing:** In Fast Mode, the routing engine will select simpler, direct analytical paths, possibly defaulting to a "general purpose" or "basic analysis" scenario class, unless a more specific (and light-weight) scenario is clearly implied. It will actively suppress debate initiation unless explicitly invoked.
*   **Rigor Mode Routing:** In Rigor Mode, the routing engine will meticulously determine the appropriate scenario class, activating all relevant complex analytical paths, internal debate mechanisms, and strict confidence gating.

This explicit distinction at the routing engine input is crucial for maintaining the integrity and distinct operational characteristics of both modes.