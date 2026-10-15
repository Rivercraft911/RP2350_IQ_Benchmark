# 16APSK normalization audit

The present coordinate alphabet is compatible with the explicit **R2=1 constellation option** in ETSI EN302307-1 V1.4.1. It is not the globally rescaled E=1 whole-frame option. No fifth coordinate magnitude is required for the R2=1 mode.

Primary source: [ETSI EN302307-1 V1.4.1](https://www.etsi.org/deliver/etsi_en/302300_302399/30230701/01.04.01_60/en_30230701v010401p.pdf), §5.4.3 printedp28; §5.5.2 printedp32; §5.5.3 printedp33. The data-constellation clause admits either unit average data-symbol energy or unit outer radius. The separate header and pilot clauses define unit-radius symbols with coordinate magnitudes1/sqrt2. Read together, the unit-outer-radius option shares its outer diagonal coordinate with those header/pilot symbols. The quarter-turn PL scrambler changes signs/swaps coordinates and does not create a new magnitude. This conclusion concerns the stated option and coordinate geometry, not implementation completeness.

For gamma=R2/R1 with R2=1, the data average energy is Edata=(3+gamma^-2)/4. At gamma2.7 this is0.78429355, so data RMS radius is0.88560349. A unit-radius header/pilot is consequently1.05521dB above average data-symbol energy. That is the consequence of this permitted amplitude choice; it should be explicit in future mode metadata and RF/AGC evaluation.

For the other allowed choice, Edata=1, R2=sqrt(4/(3+gamma^-2)). If the ENTIRE E=1 PLFRAME is then uniformly scaled to make R2=1, header/pilot coordinate magnitude becomes sqrt((3+gamma^-2)/8), about0.62621624 for gamma2.7. That is indeed a fifth positive magnitude alongside outer cos15°, outer sin15°, outer1/sqrt2, and inner1/(gamma sqrt2). The current3-bit coordinate alphabet cannot represent that distinct whole-frame normalization. It must not silently be used for that mode. A separate bulk binary-QPSK table plus mixed-region handling, or a marked-symbol linear correction, remains an optional future design if globally scaled E=1 interoperability is desired; it is not a repair required for R2=1.

Final scope for current iq16chunk / iq16native8:

- Exact16APSK data coordinates and table/shaper math for supported gamma ratios, with R2=1.
- Unit-radius PLHEADER/pilot coordinate geometry is representable without enlarging this alphabet, per the R2=1 option.
- Current benchmark emits random constellation data and repeated default history, not an actual complete16APSK PLFRAME. A complete4-column interleaver/mapper, MODCOD18..23 signalling, Gold scrambling and pilot insertion, frame transitions, full encoder/input/output deadlines, capture verification and analog qualification remain separate work.
- Native L8 component parent-reported Pico CRC PASS at128/150MHz: native_burst21.1056cycles/symbol; native_C22.5541; preceding L9burst23.8742. These component measurements do not qualify6.25Msym/s with all modem work.

Independent audit did not find an amplitude-normalization defect in the selected R2=1 option. The original E=1 counterexample was useful because it exposed an implicit mode choice; labeling that choice resolves the ambiguity without changing the current CRC-qualified component.
