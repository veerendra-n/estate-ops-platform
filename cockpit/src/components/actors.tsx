import { Bot, Cpu, Radio, ShieldCheck, Sparkles, User, Workflow } from "lucide-react";
import { actorKind, actorLabel } from "../format";

export function ActorAvatar({ actor, size = 28 }: { actor: string; size?: number }) {
  const k = actorKind(actor);
  const name = actor.split(":")[1] ?? "";
  const s = Math.round(size * 0.5);
  let icon;
  let tone = k as string;
  if (k === "engineer") icon = <User size={s} />;
  else if (k === "source") icon = <Radio size={s} />;
  else if (k === "agent") {
    if (name === "copilot") {
      icon = <Sparkles size={s} />;
      tone = "copilot";
    } else if (name === "verifier") icon = <ShieldCheck size={s} />;
    else icon = <Bot size={s} />;
  } else if (actor === "policy") {
    icon = <ShieldCheck size={s} />;
    tone = "policy";
  } else if (actor === "triage") icon = <Workflow size={s} />;
  else icon = <Cpu size={s} />;
  const initials = k === "engineer" ? name.slice(0, 1).toUpperCase() : null;
  return (
    <span className={`actor-av av-${tone}`} style={{ width: size, height: size }} title={actorLabel(actor)}>
      {initials ?? icon}
    </span>
  );
}
