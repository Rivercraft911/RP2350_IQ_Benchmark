# RP2350 I/Q waveform benchmark

Experiment: can an RP2350 replace the FPGA as the baseband waveform generator (framed, coded bits in; pulse-shaped QPSK I/Q samples out to a TI AFE7071) for the IREC PigeonVision video downlink or a SATS picture/file downlink? The goal is an honest answer, not a forced positive result.

Instructions below are collected from `../IREC/AGENTS.md` and `../SATS/AGENTS.md` (handbook-specific items omitted). Project context lives in `../IREC/Pigeon_Vision/DESIGN.md` (section 4, AFE7071 comparison), `../SATS/Next Satellite/architecture.md` (TT&C allocation) and `../SATS/Next Satellite/research/mcu-qpsk-feasibility.json`.

## Engineering
- Reason from first principles. Start with the physical behavior, requirements, and constraints; then derive the equations. Check units, signs, current paths, and limiting cases.
- Verify exact part numbers, package pins, and implementation against manufacturer datasheets. Trace actual connections rather than trusting labels or a familiar-looking circuit.
- Distinguish facts, assumptions, estimates, and unknowns. Separate operating specifications from absolute maximum ratings.
- Separate observed facts, user reports, hypotheses, proposed practices, and adopted requirements. Cite the exact revision, section, or page when it has been checked; never invent citations.
- Distinguish analytical predictions, simulation results, and measurements. State model limitations and specify how a design claim will be checked on hardware.
- Preserve calculations with units, assumptions, input provenance, tool versions and conclusions. Keep calculations simple and traceable. Do not substitute automated GOOD/PASS labels for reasoning.
- Point out errors and questionable assumptions directly, including the user's and your own earlier conclusions. Do not invent a problem to sound thorough. Recheck disputed findings independently.
- Derive margins from the mission and component evidence, not unexplained rules of thumb.

## Communication
- Lead with the finding or result. Clear, short sentences, concrete technical language.
- Avoid filler, flattery, sales language and stock AI phrasing.
- State what was checked and what remains unverified. A digital benchmark does not prove RF performance or link closure.

## Files and changes
- Keep this experiment small and organized: `reference/` (Python model, vectors), `firmware/` (Pico SDK), `host/` (validation), `results/` (measured logs), `docs/` (sources, notes).
- Use relative paths. Preserve existing work.
- Git: do not add Claude/AI co-author trailers or "Generated with" lines to commits or pull requests.
