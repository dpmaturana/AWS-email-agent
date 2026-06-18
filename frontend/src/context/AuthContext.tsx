import { createContext, useContext, useState, useCallback, useEffect, type ReactNode } from 'react'
import { Amplify } from 'aws-amplify'
import { signIn as amplifySignIn, signOut as amplifySignOut, getCurrentUser } from 'aws-amplify/auth'
import { amplifyConfig } from '../amplify-config'

const USE_COGNITO =
  !!import.meta.env.VITE_COGNITO_USER_POOL_ID &&
  !!import.meta.env.VITE_COGNITO_CLIENT_ID

if (USE_COGNITO) {
  Amplify.configure(amplifyConfig)
}

type AuthUser = { email: string }

type AuthContextType = {
  user: AuthUser | null
  isLoading: boolean
  signIn: (email: string, password: string) => Promise<void>
  signOut: () => void
}

const AuthContext = createContext<AuthContextType | null>(null)

const MOCK_CREDENTIALS = { email: 'approver@university.edu', password: 'Admin1234!' }

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null)
  const [isLoading, setIsLoading] = useState(true)

  // On mount: restore session
  useEffect(() => {
    if (USE_COGNITO) {
      getCurrentUser()
        .then(u => setUser({ email: u.signInDetails?.loginId ?? u.username }))
        .catch(() => setUser(null))
        .finally(() => setIsLoading(false))
    } else {
      const stored = localStorage.getItem('auth_user')
      setUser(stored ? (JSON.parse(stored) as AuthUser) : null)
      setIsLoading(false)
    }
  }, [])

  const signIn = useCallback(async (email: string, password: string) => {
    setIsLoading(true)
    try {
      if (USE_COGNITO) {
        await amplifySignIn({
          username: email,
          password,
          options: { authFlowType: 'USER_PASSWORD_AUTH' },
        })
        const u = await getCurrentUser()
        setUser({ email: u.signInDetails?.loginId ?? email })
      } else {
        await new Promise(r => setTimeout(r, 600))
        if (email !== MOCK_CREDENTIALS.email || password !== MOCK_CREDENTIALS.password) {
          throw new Error('Invalid credentials')
        }
        const authUser = { email }
        localStorage.setItem('auth_user', JSON.stringify(authUser))
        setUser(authUser)
      }
    } finally {
      setIsLoading(false)
    }
  }, [])

  const signOut = useCallback(async () => {
    if (USE_COGNITO) {
      await amplifySignOut()
    } else {
      localStorage.removeItem('auth_user')
      localStorage.removeItem('id_token')
    }
    setUser(null)
  }, [])

  return (
    <AuthContext.Provider value={{ user, isLoading, signIn, signOut }}>
      {isLoading ? (
        <div className="min-h-screen flex items-center justify-center text-gray-400 text-sm">
          Loading…
        </div>
      ) : children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used inside AuthProvider')
  return ctx
}
