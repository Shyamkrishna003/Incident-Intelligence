import type { Verdict } from "./types";

export const VERDICT_LABEL: Record<Verdict, string> = {
  correct: "Correct",
  partially_correct: "Partially correct",
  incorrect: "Incorrect",
};

export const VERDICTS: Verdict[] = ["correct", "partially_correct", "incorrect"];
