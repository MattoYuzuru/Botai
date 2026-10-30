import type { AgentUiBlock } from "@/api/agent-ui.gen";
import type { AttachmentBlock, PlanBlock, TaskBlock } from "@/api/v3";
import type { TextBlock, UnknownBlock } from "@/features/jobs/blocks";

export type Choice = Extract<AgentUiBlock, { type: "choice" }>;
export type InputBlock = Extract<AgentUiBlock, { type: "input" }>;
export type Confirm = Extract<AgentUiBlock, { type: "confirm" }>;
export type Summary = Extract<AgentUiBlock, { type: "summary" }>;
export type Approval = Extract<AgentUiBlock, { type: "approval" }>;
export type Login = Extract<AgentUiBlock, { type: "login" }>;
export type Wait = Extract<AgentUiBlock, { type: "wait" }>;
export type Handoff = Extract<AgentUiBlock, { type: "handoff" }>;
export type { AttachmentBlock, PlanBlock, TaskBlock, TextBlock };
export type Unknown = UnknownBlock;
