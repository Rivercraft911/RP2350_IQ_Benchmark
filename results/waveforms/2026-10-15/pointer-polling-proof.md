# Quadlive polling128us, explicit256us limit and credit runway

Source freeze: /tmp/yapogee-quadlive-poll128-source.json. Only bench_quadlive.c, host/quadlive_trial.py and host/test_quadlive_trial.py changed. Original24/32-us source snapshot, raw quick/soak records and first freeze reports remain intact.

## Diagnosis of the first two runs

The first128MHz/cpw2 positive run stopped after58us with max begin gap42us. Its longest measured poll body was4849cycles=37.883us;5752cycles=44.938us is the SUM of two bodies, not a45-us maximum body. The next fault1 run stopped after47us with max begin gap35us and max body4280cycles=33.438us. Pointer/output faults were0, source read counts remained inside grants, CRCs and restoration passed. The primary error was the artificial32-us bench ceiling. Positive ERR_CAPTURE and negative ERR_BOUNDARY were secondary missing-capture / missing-expected-starvation outcomes after that early abort. These were not evidence of an actual source-ownership or DMA-throughput failure.

First-use XIP/cache traffic is a plausible cause of the long initial body, since publish/loop paths are newly executed after large setup paths. The existing records do not isolate cache misses, IRQ work or a specific instruction stall. Do not report the cache hypothesis as established causal evidence. Later24/32-us runs and60-second16/18.75-Msym trials remain their own immutable evidence.

## Sufficient bounds

The L8 pointer program requires49PIO cycles per complete16-symbol raw input word per axis. Retain the existing8-word source burst/retirement reserve. The timer reports integer microseconds. An accepted integer delta<=256us admits a real interval<257us, so include that extra microsecond:

`B(clock) = 8 + ceil(clock_hz * (256+1) / (49 * 1e6))`.

At128MHz B=680words; at150MHz B=795. The existing8192-word source cursor deadline is `(8192-8)*49*1e6/clock`:3132us or2673us.256us is less than one eighth of either deadline, and at most795<8192words can advance over the allowed interval. All four cpw1/2 ×128/150 configurations share this conservative producer bound; slower paired output cannot consume input faster than the PIO program's minimum instruction count.

At startup and after every normal publication, require remaining finite credits `granted_words-input_high_words >= 2*B`, using zero if issued already exceeds grants. Required runway is1360words at128MHz,1590 at150MHz. The runtime guard directly rejects arbitrary skew or publication state that leaves less runway, without relying on the healthy-axis skew estimate. It is disabled only after deliberatefault1 grant freeze; the zero terminal credit continues to prevent any ungranted source read.

The period target is128us. The explicit256-us failure limit covers both poll-begin gaps AND a more conservative source snapshot span: time AFTER current progress/publish minus time BEFORE the PREVIOUS progress/publish. This upper-bounds the elapsed time between either axis's two actual READ_ADDR reads, even if cold code/IRQs delay execution INSIDE a poll. The final active snapshot is checked too. Checking only begin-to-begin times could overlook the last long current body. The maximum observed span and the derived issue/credit/cursor bounds are recorded and independently validated by the host.

Induction: after a checked snapshot/publication at least2B credits remain. Before the next accepted snapshot at mostB additional reads can issue, leaving at leastB prior credits; publication replenishes only within the already committed, guarded prefix. A delayed call exceeding256us fails qualification; the existing producer's own cursor deadline and hardware zero sentinel continue to protect input ownership. This changes the supervision schedule, not the immutable source data, PIO program, table mapping, output clock or sample arithmetic.

## Healthy minimum, separate from runtime rejection

For coupled fault-free paired outputs, each source read cursor leads physical output by at most the existing128-symbol bound. Round up an additional word for opposite-axis phase: max9source-word skew. Let R=min(issueI,issueQ)-8 and prior R advance by at mostB. Publication is conservatively previousR+8192. The15-valid512-word-credit limit plus zero sentinel gives remaining credit lower bound

`min(15*512 -511 -8 -9, 8192 -B -511 -8 -9)`

`= min(7152,7664-B)`, yielding6984words at128MHz and6869 at150MHz. Both exceed required2B by a wide margin. This estimate is for healthy coupled consumers. Independent arbitrary FIFO drains have no such skew bound; they are covered by the direct runtime runway rejection and finite hardware credit gating.

## Validation

Eleven host/native tests pass, including explicit NaN/+infinity/-infinity mutations of symbol rate and both reported CPU fractions. All must fail; finite checks precede tolerance comparisons.98,304 actual-C grant-leaf cases cover all16credit-ring phases, all512source/batch phases, advances0/1/B and skews0/9 at both clocks. They attain exactly6984/6869 minimum runway, keep the zero sentinel, and pass the actual compiled C guard.56 arbitrary-skew/underflow cases verify rejection below2B, acceptance at/above2B, tracked minimum and64-bit count bases.98,304 actual cursor-extension cases remain checked. All8192 sparse waveform observations are checked at one fixed offset for both cpw values and both clocks; corruption, metadata drift, deadline violation, bad firmware identity/clock reply and finite-prefix violations fail.

Real SDK2.2/ArmGCC14.2 isolated -Werror compilation passes. Bench stack808B, text4888B,rodata2340B,data/BSS0. No USB, shared build or commit by this agent. Current source/model checks are not new hardware throughput evidence. Root will build/flash and compare identical PRBS captures and instrumented work with the retained24/32-us baseline. The inherited output backend abort wait remains unbounded as previously disclosed.
