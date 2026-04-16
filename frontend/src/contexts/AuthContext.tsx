import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { api } from "../lib/api";
import { auth, onAuthStateChanged, type User } from "../lib/firebase";

interface AuthContextType {
  user: User | null;
  loading: boolean;
}

const AuthContext = createContext<AuthContextType>({ user: null, loading: true });

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser]       = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const unsubscribe = onAuthStateChanged(auth, async (u) => {
      if (u) {
        try {
          const idToken = await u.getIdToken();
          const { data } = await api.post("/auth/google", { firebase_token: idToken });
          localStorage.setItem("access_token", data.access_token);
          setUser(u);
        } catch (err) {
          console.error("Backend auth exchange failed:", err);
          localStorage.removeItem("access_token");
          setUser(null);
        }
      } else {
        localStorage.removeItem("access_token");
        setUser(null);
      }
      setLoading(false);
    });
    return unsubscribe;
  }, []);

  if (loading) {
    return (
      <div style={{
        display:         "flex",
        alignItems:      "center",
        justifyContent:  "center",
        height:          "100vh",
        backgroundColor: "#06060b",
        color:           "#3B82F6",
        fontFamily:      "'JetBrains Mono', monospace",
        fontSize:        "0.75rem",
        letterSpacing:   "0.12em",
      }}>
        ◈ CUSTOMS DECL — Authenticating…
      </div>
    );
  }

  return (
    <AuthContext.Provider value={{ user, loading }}>
      {children}
    </AuthContext.Provider>
  );
}

export const useAuth = () => useContext(AuthContext);
