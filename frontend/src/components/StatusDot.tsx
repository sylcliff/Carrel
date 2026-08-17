import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";

/** Processing status -> tailwind dot color. */
export function statusColor(s: string): string {
  if (s === "ready" || s === "summarized") return "bg-green-500";
  if (s === "parsed") return "bg-blue-500";
  if (s === "pdf_ready") return "bg-yellow-500";
  if (s === "failed") return "bg-red-500";
  return "bg-gray-400";
}

const STATUS_LABELS: Record<string, { label: string; desc: string }> = {
  ready: { label: "就绪", desc: "已分块并生成向量，可阅读与检索" },
  summarized: { label: "已摘要", desc: "中文摘要已生成，待分块嵌入" },
  parsed: { label: "已解析", desc: "PDF 已转为 Markdown，待生成摘要" },
  pdf_ready: { label: "已下载", desc: "PDF 已下载，等待解析" },
  failed: { label: "失败", desc: "处理失败（重试后仍未成功）" },
  pending: { label: "待处理", desc: "仅有元数据，排队等待下载" },
};

function statusText(s: string) {
  return STATUS_LABELS[s] ?? { label: s, desc: "未知状态" };
}

export function StatusDot({ s }: { s: string }) {
  const { label, desc } = statusText(s);
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          className={`inline-block h-2 w-2 shrink-0 rounded-full ${statusColor(s)}`}
          aria-label={`状态：${label}`}
        />
      </TooltipTrigger>
      <TooltipContent>
        <div className="font-medium">
          {label} <span className="opacity-60">({s})</span>
        </div>
        <div className="mt-0.5 opacity-80">{desc}</div>
      </TooltipContent>
    </Tooltip>
  );
}
