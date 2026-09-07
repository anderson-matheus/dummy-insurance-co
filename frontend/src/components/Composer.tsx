import { FormEvent, KeyboardEvent, useState } from "react";
import { S } from "../strings";

interface Props {
  disabled: boolean;
  busy: boolean;
  onSend: (content: string) => Promise<boolean>;
  onCancel: () => void;
}

export function Composer({ disabled, busy, onSend, onCancel }: Props) {
  const [value, setValue] = useState("");

  const submit = async (e?: FormEvent) => {
    e?.preventDefault();
    const content = value.trim();
    if (!content || busy || disabled) return;
    const sent = await onSend(content);
    if (sent) setValue("");
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void submit();
    }
  };

  return (
    <form className="composer" onSubmit={(e) => void submit(e)}>
      <textarea
        className="composer__input"
        placeholder={S.composerPlaceholder}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={onKey}
        rows={2}
        disabled={disabled}
        aria-label={S.composerPlaceholder}
      />
      {busy ? (
        <button type="button" className="btn btn--danger" onClick={onCancel}>
          {S.cancel}
        </button>
      ) : (
        <button type="submit" className="btn btn--primary" disabled={disabled || !value.trim()}>
          {S.send}
        </button>
      )}
    </form>
  );
}
