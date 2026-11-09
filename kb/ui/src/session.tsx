import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, getToken, setToken, type Me } from "./api";

interface Session {
  me: Me | null;
  loading: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => void;
}

const Ctx = createContext<Session | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState<boolean>(!!getToken());

  useEffect(() => {
    if (!getToken()) return;
    api
      .get<Me>("/me")
      .then(setMe)
      .catch(() => setToken(null))
      .finally(() => setLoading(false));
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    const t = await api.post<{ access_token: string }>("/auth/login", { username, password });
    setToken(t.access_token);
    setMe(await api.get<Me>("/me"));
  }, []);

  const logout = useCallback(() => {
    setToken(null);
    setMe(null);
  }, []);

  return <Ctx.Provider value={{ me, loading, login, logout }}>{children}</Ctx.Provider>;
}

export function useSession(): Session {
  const s = useContext(Ctx);
  if (!s) throw new Error("useSession outside SessionProvider");
  return s;
}
