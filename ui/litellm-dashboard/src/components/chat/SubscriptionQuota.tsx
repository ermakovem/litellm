"use client";

import { FormEvent, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { isProxyAdminRole } from "@/utils/roles";
import { getGlobalLitellmHeaderName, getProxyBaseUrl } from "../networking";
import { useChatShell } from "@/contexts/ChatShellContext";

type Window = {
  source: string;
  window: string;
  remaining_percent: number;
  resets_at: number;
  observed_at: number;
  provider_remaining_percent?: number;
  unallocated_points?: number;
};

type Share = { source: string; user_id: string; percent: number; windows?: Window[] };

async function quotaRequest<T>(token: string, path: string, payload?: Share): Promise<T | null> {
  const response = await fetch(`${getProxyBaseUrl()}/subscription-quota/${path}`, {
    method: payload ? "PUT" : "GET",
    headers: { [getGlobalLitellmHeaderName()]: `Bearer ${token}`, "Content-Type": "application/json" },
    body: payload ? JSON.stringify(payload) : undefined,
  });
  if (!payload && response.status === 404) return null;
  if (!response.ok) throw new Error("Could not access subscription quota");
  return response.json() as Promise<T>;
}

function formatPercent(percent: number) {
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(percent) + "%";
}

function formatTime(seconds: number) {
  return new Date(seconds * 1000).toLocaleString();
}

function ShareEditor({ token }: { token: string }) {
  const client = useQueryClient();
  const [source, setSource] = useState("");
  const [userId, setUserId] = useState("");
  const [percent, setPercent] = useState("");
  const shares = useQuery({
    queryKey: ["subscription-quota-shares", token],
    queryFn: () => quotaRequest<Share[]>(token, "shares"),
    refetchInterval: 15000,
    retry: false,
  });
  const save = useMutation({
    mutationFn: (share: Share) => quotaRequest(token, "shares", share),
    onSuccess: () =>
      client
        .invalidateQueries({ queryKey: ["subscription-quota"] })
        .then(() => client.invalidateQueries({ queryKey: ["subscription-quota-shares", token] })),
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    save.mutate({ source: source.trim(), user_id: userId.trim(), percent: Number(percent) });
  }

  return (
    <div className="mt-4 space-y-3 border-t pt-4">
      <h3 className="font-semibold">Manage profile shares</h3>
      <form className="flex flex-wrap items-end gap-3" onSubmit={submit}>
        <Label className="grid gap-2">
          Source
          <Input required value={source} onChange={(event) => setSource(event.target.value)} />
        </Label>
        <Label className="grid gap-2">
          User ID
          <Input required value={userId} onChange={(event) => setUserId(event.target.value)} />
        </Label>
        <Label className="grid gap-2">
          Share (%)
          <Input
            type="number"
            min={0}
            max={100}
            step="any"
            required
            value={percent}
            onChange={(event) => setPercent(event.target.value)}
          />
        </Label>
        <Button type="submit" disabled={save.isPending}>
          Save share
        </Button>
      </form>
      {save.isError && (
        <p role="alert" className="text-sm text-destructive">
          Could not save share. Check that total shares stay within 100%.
        </p>
      )}
      {save.isSuccess && (
        <p role="status" className="text-sm text-muted-foreground">
          Share saved
        </p>
      )}
      {shares.isError && (
        <p role="alert" className="text-sm text-destructive">
          Could not load shares
        </p>
      )}
      {shares.data?.map((share) => (
        <div className="flex flex-wrap items-center gap-3 text-sm" key={`${share.source}/${share.user_id}`}>
          <span>
            {share.source} · {share.user_id} · {formatPercent(share.percent)} assigned
          </span>
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              setSource(share.source);
              setUserId(share.user_id);
              setPercent(String(share.percent));
            }}
          >
            Edit
          </Button>
          {share.windows?.map((window) => (
            <span className="text-muted-foreground" key={window.window}>
              {window.window} · {formatPercent(window.remaining_percent)} personal ·{" "}
              {formatPercent(window.provider_remaining_percent ?? 0)} provider ·{" "}
              {(window.unallocated_points ?? 0).toFixed(2)} pp unallocated
            </span>
          ))}
        </div>
      ))}
    </div>
  );
}

export function useSubscriptionQuota(accessToken: string) {
  return useQuery({
    queryKey: ["subscription-quota", accessToken],
    queryFn: () => quotaRequest<Window[]>(accessToken, "me"),
    enabled: !!accessToken,
    refetchInterval: 15000,
    retry: false,
  });
}

export function SubscriptionQuota({ quota }: { quota: ReturnType<typeof useSubscriptionQuota> }) {
  const { accessToken, userRole } = useChatShell();
  const admin = isProxyAdminRole(userRole);
  if (quota.isError) {
    return (
      <p role="alert" className="mb-4 text-sm text-destructive">
        Could not load subscription quota
      </p>
    );
  }
  if (!quota.data || (!quota.data.length && !admin)) return null;

  return (
    <section className="mb-5 rounded-lg border bg-card p-4" aria-label="Subscription quota">
      <h2 className="mb-3 font-semibold">Subscription quota</h2>
      <div className="grid gap-3 sm:grid-cols-2">
        {quota.data.map((window) => (
          <div key={`${window.source}/${window.window}`} className="rounded-lg border p-3">
            <h3 className="text-sm font-medium">
              {window.source} · {window.window}
            </h3>
            <p className="my-1 text-xl font-semibold">{formatPercent(window.remaining_percent)} remaining</p>
            <p className="text-sm text-muted-foreground">
              Estimated from usage reported at {formatTime(window.observed_at)}
            </p>
            <p className="text-sm text-muted-foreground">Reset {formatTime(window.resets_at)}</p>
          </div>
        ))}
      </div>
      {admin && <ShareEditor token={accessToken} />}
    </section>
  );
}
