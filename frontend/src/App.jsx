import { Routes, Route, NavLink } from 'react-router-dom'
import Chat from './Chat'
import Dashboard from './Dashboard'

export default function App() {
  return (
    <div className="app">
      <nav className="nav">
        <span className="nav-brand">Finance Spend Intelligence</span>
        <div className="nav-links">
          <NavLink
            to="/"
            end
            className={({ isActive }) => isActive ? 'nav-link active' : 'nav-link'}
          >
            Chat
          </NavLink>
          <NavLink
            to="/dashboard"
            className={({ isActive }) => isActive ? 'nav-link active' : 'nav-link'}
          >
            Dashboard
          </NavLink>
        </div>
      </nav>
      <main className="main">
        <Routes>
          <Route path="/" element={<Chat />} />
          <Route path="/dashboard" element={<Dashboard />} />
        </Routes>
      </main>
    </div>
  )
}
