import { request } from "./base";
import type { Thread, ThreadMessage, ThreadUpdatePayload } from "./types";

type ThreadCreate = {
  fiche_id: number;
  title: string;
  thread_type: string;
  memory_strategy: string;
  active: boolean;
};

type ThreadMessageCreate = {
  role: "user";
  content: string;
};

export async function fetchThreads(ficheId: number, threadType?: string): Promise<Thread[]> {
  const params = new URLSearchParams({ fiche_id: String(ficheId) });
  if (threadType) {
    params.append("thread_type", threadType);
  }
  return request<Thread[]>(`/threads?${params.toString()}`);
}

export async function fetchThreadByTitle(title: string): Promise<Thread | null> {
  const params = new URLSearchParams({ title });
  const threads = await request<Thread[]>(`/threads?${params.toString()}`);
  return threads.length > 0 ? threads[0] : null;
}

export async function fetchThreadMessages(threadId: number): Promise<ThreadMessage[]> {
  return request<ThreadMessage[]>(`/threads/${threadId}/messages`);
}

export async function createThread(ficheId: number, title: string): Promise<Thread> {
  const payload: ThreadCreate = {
    fiche_id: ficheId,
    title,
    thread_type: "chat",
    memory_strategy: "buffer",
    active: true,
  };
  return request<Thread>(`/threads`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function postThreadMessage(threadId: number, content: string): Promise<ThreadMessage> {
  const payload: ThreadMessageCreate = {
    role: "user",
    content,
  };
  return request<ThreadMessage>(`/threads/${threadId}/messages`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function startThreadCourse(threadId: number): Promise<void> {
  await request<void>(`/threads/${threadId}/courses`, {
    method: "POST",
  });
}

export async function updateThread(threadId: number, payload: ThreadUpdatePayload): Promise<Thread> {
  return request<Thread>(`/threads/${threadId}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}
