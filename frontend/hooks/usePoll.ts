"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, Job } from "@/lib/api";

/** Charge une ressource et la recharge toutes les `interval` ms tant que `active(data)` est vrai. */
export function usePoll<T>(loader: () => Promise<T>, active: (data: T | undefined) => boolean, interval = 2500) {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<string>();
  const loaderRef = useRef(loader);
  loaderRef.current = loader;

  const reload = useCallback(async () => {
    try {
      setData(await loaderRef.current());
      setError(undefined);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  useEffect(() => {
    if (!active(data)) return;
    const t = setInterval(reload, interval);
    return () => clearInterval(t);
  }, [data, active, interval, reload]);

  return { data, error, reload, setData };
}

/** Attend la fin d'une tâche de la file. */
export async function waitForJob(job: Job, onTick?: (j: Job) => void, interval = 2000): Promise<Job> {
  let current = job;
  while (current.status === "queued" || current.status === "running") {
    await new Promise((r) => setTimeout(r, interval));
    current = await api.job(current.id);
    onTick?.(current);
  }
  return current;
}
