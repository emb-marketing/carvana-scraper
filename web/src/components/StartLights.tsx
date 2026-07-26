"use client";

import { useEffect, useState } from "react";

/**
 * The F1 start sequence: five lights fill red left to right, then all cut to green.
 *
 * Shown once when a run is queued. It is decoration with a job — queuing is asynchronous and can
 * sit for a while before a worker picks it up, so the sequence gives the submit a beat of
 * acknowledgement instead of a form that just goes quiet.
 */
export function StartLights({ onFinished }: { onFinished?: () => void }) {
  const [phase, setPhase] = useState<"arming" | "go">("arming");

  useEffect(() => {
    const toGreen = setTimeout(() => setPhase("go"), 1500);
    const done = setTimeout(() => onFinished?.(), 2400);
    return () => {
      clearTimeout(toGreen);
      clearTimeout(done);
    };
  }, [onFinished]);

  return (
    <div className={`lights ${phase}`} role="status" aria-label="Run queued">
      {[0, 1, 2, 3, 4].map((index) => (
        <span className="lamp" key={index} />
      ))}
    </div>
  );
}
