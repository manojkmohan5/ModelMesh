# Model profiles: what each developer says its model is for

Public information about the 12 Lumen models, checked 2026-11-06. **Every score here is the
developer's own claim**; we found no independent replication. Our measurements are in
[MODEL-BENCHMARKS.md](MODEL-BENCHMARKS.md); where they disagree with these claims, trust the
measurements for routing.

Confidence: **official** means the developer's model card or post; **third party** means press or
another host.

| Lumen id | Developer and release | Architecture | Context (developer) |
|---|---|---|---|
| beam-1.0 | Reflection AI, 2026-10 | MoE, 501B total / 23B active | 1M |
| deepseek-v4-flash | DeepSeek, 2026-04-24 (preview) | MoE, 284B / 13B | 1M |
| deepseek-v4.1-flash | DeepSeek, 2026-09-10 | MoE encoder-decoder, 552B; 8B active reading, 16B writing | 1M |
| gemma-4-31b-it | Google DeepMind, 2026-04-02 | Dense, 30.7B | 256K |
| glm-5.3 | Z.ai, 2026-08 | MoE, about 744B / 40B (third party) | 1M; 128K output |
| muse-glimmer-30b | Meta Superintelligence Lab, 2026-08-10 (third-party date) | Dense, about 29.6B including the vision encoder | 131K+ |
| nemotron-3-super-120b-a12b | NVIDIA, 2026-03-11 | Hybrid Mamba-2/attention MoE, 120B / 12B | Up to 1M (default 256K) |
| ornith-1.0-35b | DeepReinforce (Ornith AI), 2026-06 | 35B MoE; third parties say an RL post-train of Qwen3.5-35B-A3B | 262K |
| qwen3-coder-next | Qwen (Alibaba), 2026-02-03 | Hybrid-attention MoE, 80B / 3B | 262K |
| qwen3.6-35b-a3b | Qwen, 2026-04-16 | MoE, 35B / 3B | 262K (about 1M with YaRN) |
| qwen3.8-27b | Qwen, 2026-08 | Dense vision-language, 27B | 262K (1M extended) |
| qwen3.8-flash | Qwen, 2026-08-26 (third-party date) | Hosted build of open Qwen3.8-Flash-Next: MoE, 125B / 6B | 1M hosted; 262K open weights |

## Per model

**beam-1.0** (official). Software engineering, agentic tool use, math and science, with "token-efficient
reasoning" (about 3x fewer reasoning tokens than leading open models). Text only, mainly English.
SWE-bench Verified 81.6; Terminal-Bench 2.1 80.1; AIME 2026 97.7; GPQA Diamond 90.6; MCP Atlas 80.8.
Sources: https://reflection.ai/beam-model-card, https://reflection.ai/blog/beam-is-available

**deepseek-v4-flash** (official). Reasoning that "closely approaches V4-Pro" at lower cost; matches Pro on
simple agent tasks, trails it on knowledge and the hardest agent work. Text only; non-think, high and max
modes. Max mode: LiveCodeBench 91.6; Codeforces 3052; SWE-bench Verified 79.0; GPQA Diamond 88.1; HMMT Feb
2026 94.8; MRCR 1M 78.7. Sources: https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash,
https://api-docs.deepseek.com/news/news260424. Note: DeepSeek's own API now routes this id to V4.1-Flash;
confirm which weights Lumen serves.

**deepseek-v4.1-flash** (official). Cost-efficient, input-heavy agent work; native image understanding;
reasoning effort from 1 to 100. Effort 100: Terminal-Bench 2.1 90.6; DeepSWE v1.1 74.2; GPQA Diamond 90.9;
Codeforces 3471; CyberGym 88.1. Sources: https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash,
https://api-docs.deepseek.com/news/news260910/

**gemma-4-31b-it** (official). Highest-quality Gemma 4 model and a base for fine-tuning: reasoning, coding,
function calling and JSON, 140+ languages, image input. #3 among open models on the Arena AI text
leaderboard at launch. MMLU-Pro 85.2; AIME 2026 89.2; LiveCodeBench v6 80.0; GPQA Diamond 84.3; τ2 76.9;
MMMU-Pro 76.9. Sources: https://huggingface.co/google/gemma-4-31B-it,
https://blog.google/innovation-and-ai/technology/developers-tools/gemma-4/

**glm-5.3** (official scores; third-party dates and size). Complex software engineering, long-horizon tasks,
finding security vulnerabilities. Text only; always reasons (low, high, max). Terminal-Bench 2.1 88.2;
Terminal-Bench 3.0 28.3; DeepSWE 66.9; CyberGym 84.5; Toolathlon Verified 73.0. Sources:
https://huggingface.co/zai-org/GLM-5.3, https://docs.z.ai/guides/llm/glm-5.3,
https://gigazine.net/gsc_news/en/20260829-glm-5-3-open/

**muse-glimmer-30b** (official card; third-party date). "Purpose-built for autonomous agentic tasks on
consumer hardware"; distilled from Muse Spark; text and images; reasoning from low to xhigh. High reasoning:
MCP Atlas 75.5; SWE-bench Verified 76.0; SWE-bench Pro 51.2; OSWorld-Verified 65.9; AIME 2026 94.7;
AA-LCR 80.0. Sources: https://huggingface.co/meta-models/Muse-Glimmer-30B,
https://phoronix.com/news/Meta-Muse-Glimmer

**nemotron-3-super-120b-a12b** (official). Agent workflows, long-context reasoning, tool use, retrieval
(RAG), high-volume workloads; 7 languages. RULER at 1M 91.75; AIME25 90.21; LiveCodeBench 81.19; SWE-bench
(OpenHands) 60.47; τ2 average 61.15. Source: https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-BF16

**ornith-1.0-35b** (official scores; third-party base model). Agentic coding (terminal, software
engineering, whole repositories), trained with self-improving RL that learns its own task scaffolds. The
developer lists image input; Lumen lists text only. SWE-bench Verified 75.6; Terminal-Bench 2.1 (Terminus-2)
64.2; SWE-bench Pro 50.4; SWE-bench Multilingual 69.3. Ornith-1.5 has since been released. Sources:
https://ornith.ai/ornith_1_0.html, https://huggingface.co/deepreinforce-ai/Ornith-1.0-35B

**qwen3-coder-next** (official). Coding agents and local development: long-horizon work, tool calling,
recovering from failed runs, use inside coding tools. No thinking mode (Lumen flags it otherwise).
SWE-bench Verified 70.6; SWE-bench Pro 44.3; SWE-bench Multilingual 64.3; Terminal-Bench 2.0 36.2. Source:
https://huggingface.co/Qwen/Qwen3-Coder-Next

**qwen3.6-35b-a3b** (official). Agentic coding, especially front end, and reasoning across a whole
repository; text, images and video per the developer (Lumen lists text only). SWE-bench Verified 73.4;
Terminal-Bench 2.0 51.5; AIME26 92.7; GPQA 86.0; MMMU 81.7; VideoMMMU 83.7. Source:
https://huggingface.co/Qwen/Qwen3.6-35B-A3B

**qwen3.8-27b** (official). Coding, long-horizon agent tasks, computer and app use, vision; text, images and
video. Terminal-Bench 2.1 73.0; SWE-bench Pro 61.7; LiveCodeBench v6 90.3; GPQA Diamond 89.2;
OSWorld-Verified 84.3; AndroidWorld 81.9. Source: https://huggingface.co/Qwen/Qwen3.8-27B

**qwen3.8-flash** (official specs; third-party date). "Shines in coding assistance, agentic workflows, and
visual understanding", including desktop apps, charts and video up to 2 hours; thinking mode and built-in
tools. Flash-Next card: SWE-bench Pro 62.5; SWE-bench Multilingual 81.0; GPQA Diamond 91.7; LiveCodeBench v6
91.9; AndroidWorld 84.5; LVBench 76.6. Sources:
https://docs.modelstudio.console.alibabacloud.com/en/model-studio/qwen3-8-flash,
https://huggingface.co/Qwen/Qwen3.8-Flash-Next

## Differences from Lumen's model list

- **Thinking flag:** `qwen3-coder-next` has no thinking mode, though Lumen flags it; `glm-5.3` always reasons.
- **Inputs:** developers say Gemma 4, Qwen3.6-35B-A3B and Qwen3.8-27B also take video, and Ornith takes
  images; Lumen lists video only for `qwen3.8-flash` and lists Ornith as text only. The benchmark follows
  Lumen, since that is what ModelMesh can send.
- **Context windows** below the developer's maximum (Nemotron, the Qwen models) are probably Lumen
  serving settings.
