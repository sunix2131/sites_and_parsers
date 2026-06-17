import { useState, useEffect } from 'react';
import { useAuth } from '../context/AuthContext';
import { api } from '../api/client';
import PortfolioAdmin from '../components/PortfolioAdmin';
import {
  Users, TrendingUp, UserPlus, LogOut, Search, MoreVertical,
  CheckCircle, XCircle, Clock, Phone, ChevronDown, RefreshCw,
  BarChart3, Eye, EyeOff, UserCheck, UserX, AlertCircle,
  Play, Square, Terminal, MapPin, FolderOpen
} from 'lucide-react';

interface Stats {
  total_leads: number;
  new_leads: number;
  assigned_leads: number;
  confirmed_leads: number;
  declined_leads: number;
  followup_leads: number;
  no_answer_leads: number;
  calling_leads: number;
  total_sellers: number;
  active_sellers: number;
  conversion_rate: number;
  seller_stats: Array<{
    id: number;
    name: string;
    is_active: boolean;
    assigned: number;
    confirmed: number;
    declined: number;
  }>;
}

interface Seller {
  id: number;
  username: string;
  full_name: string;
  role: string;
  is_active: boolean;
  created_at: string;
}

type Tab = 'overview' | 'sellers' | 'leads' | 'parser' | 'portfolio';

interface ParserTask {
  task_id: string;
  status: string;
  query: string;
  location: string;
  limit: number;
  mode: string;
  output?: string;
  created_at?: string;
}

export default function AdminDashboard() {
  const { user, logout } = useAuth();
  const [tab, setTab] = useState<Tab>('overview');
  const [stats, setStats] = useState<Stats | null>(null);
  const [sellers, setSellers] = useState<Seller[]>([]);
  const [loading, setLoading] = useState(true);

  const [showCreateForm, setShowCreateForm] = useState(false);
  const [newSeller, setNewSeller] = useState({ username: '', password: '', full_name: '' });
  const [creating, setCreating] = useState(false);

  const [leads, setLeads] = useState<any[]>([]);
  const [leadsLoading, setLeadsLoading] = useState(false);
  const [leadFilter, setLeadFilter] = useState('');
  const [leadSearch, setLeadSearch] = useState('');
  const [selectedLead, setSelectedLead] = useState<any>(null);
  const [assignModal, setAssignModal] = useState<number | null>(null);

  const [parserQuery, setParserQuery] = useState('');
  const [parserLocation, setParserLocation] = useState('Москва');
  const [parserLimit, setParserLimit] = useState(20);
  const [parserMode, setParserMode] = useState('scrape');
  const [parserTasks, setParserTasks] = useState<ParserTask[]>([]);
  const [parserRunning, setParserRunning] = useState(false);
  const [parserLoading, setParserLoading] = useState(false);

  const loadData = async () => {
    try {
      const [statsData, sellersData] = await Promise.all([
        api.get<Stats>('/admin/stats'),
        api.get<Seller[]>('/admin/users'),
      ]);
      setStats(statsData);
      setSellers(sellersData);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  const loadLeads = async () => {
    setLeadsLoading(true);
    try {
      const params = new URLSearchParams();
      if (leadFilter) params.set('status', leadFilter);
      if (leadSearch) params.set('search', leadSearch);
      params.set('per_page', '100');
      const data = await api.get<any[]>(`/leads?${params}`);
      setLeads(data);
    } catch (err) {
      console.error(err);
    } finally {
      setLeadsLoading(false);
    }
  };

  useEffect(() => {
    loadData();
  }, []);

  useEffect(() => {
    if (tab === 'leads') loadLeads();
  }, [tab, leadFilter]);

  const createSeller = async () => {
    if (!newSeller.username || !newSeller.password || !newSeller.full_name) return;
    setCreating(true);
    try {
      await api.post('/admin/users', { ...newSeller, role: 'seller' });
      setNewSeller({ username: '', password: '', full_name: '' });
      setShowCreateForm(false);
      await loadData();
    } catch (err: any) {
      alert(err.message);
    } finally {
      setCreating(false);
    }
  };

  const toggleSeller = async (seller: Seller) => {
    try {
      await api.patch(`/admin/users/${seller.id}`, { is_active: !seller.is_active });
      await loadData();
    } catch (err: any) {
      alert(err.message);
    }
  };

  const resetPassword = async (seller: Seller) => {
    const newPass = prompt('Новый пароль:');
    if (!newPass) return;
    try {
      await api.patch(`/admin/users/${seller.id}`, { password: newPass });
      alert('Пароль изменён');
    } catch (err: any) {
      alert(err.message);
    }
  };

  const assignLead = async (leadId: number, sellerId: number) => {
    try {
      await api.post(`/leads/${leadId}/assign`, { seller_id: sellerId });
      setAssignModal(null);
      await loadLeads();
      await loadData();
    } catch (err: any) {
      alert(err.message);
    }
  };

  const batchAssign = async (sellerId: number) => {
    const count = prompt('Сколько лидов назначить?', '20');
    if (!count) return;
    try {
      const result = await api.post<{ assigned: number }>(`/leads/batch-assign?seller_id=${sellerId}&count=${count}`);
      alert(`Назначено: ${result.assigned}`);
      await loadLeads();
      await loadData();
    } catch (err: any) {
      alert(err.message);
    }
  };

  const unassignLead = async (leadId: number) => {
    if (!confirm('Снять назначение?')) return;
    try {
      await api.post(`/leads/${leadId}/unassign`);
      await loadLeads();
      await loadData();
    } catch (err: any) {
      alert(err.message);
    }
  };

  const loadParserTasks = async () => {
    try {
      const tasks = await api.get<ParserTask[]>('/parser/tasks');
      setParserTasks(tasks);
      const hasRunning = tasks.some((t: ParserTask) => t.status === 'running' || t.status === 'pending');
      setParserRunning(hasRunning);
    } catch (err) {
      console.error(err);
    }
  };

  const runParser = async () => {
    if (!parserQuery.trim()) return;
    setParserLoading(true);
    try {
      await api.post('/parser/run', {
        query: parserQuery,
        location: parserLocation,
        limit: parserLimit,
        mode: parserMode,
      });
      await loadParserTasks();
    } catch (err: any) {
      alert(err.message);
    } finally {
      setParserLoading(false);
    }
  };

  const stopParserTask = async (taskId: string) => {
    try {
      await api.post(`/parser/stop/${taskId}`);
      await loadParserTasks();
    } catch (err: any) {
      alert(err.message);
    }
  };

  useEffect(() => {
    if (tab === 'parser') loadParserTasks();
  }, [tab]);

  useEffect(() => {
    if (tab === 'parser' && parserRunning) {
      const interval = setInterval(loadParserTasks, 3000);
      return () => clearInterval(interval);
    }
  }, [tab, parserRunning]);

  const statusColors: Record<string, string> = {
    new: 'bg-slate-100 text-slate-700',
    assigned: 'bg-blue-100 text-blue-700',
    calling: 'bg-yellow-100 text-yellow-700',
    confirmed: 'bg-emerald-100 text-emerald-700',
    declined: 'bg-red-100 text-red-700',
    followup: 'bg-purple-100 text-purple-700',
    no_answer: 'bg-orange-100 text-orange-700',
    waiting: 'bg-cyan-100 text-cyan-700',
  };

  const statusLabels: Record<string, string> = {
    new: 'Новый',
    assigned: 'Назначен',
    calling: 'В работе',
    confirmed: 'Согласен',
    declined: 'Отказ',
    followup: 'Перезвонить',
    no_answer: 'Не ответил',
    waiting: 'Ждёт',
  };

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary-600"></div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-slate-50">
      <header className="bg-white border-b border-slate-200 sticky top-0 z-40">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex items-center justify-between h-16">
            <div className="flex items-center gap-3">
              <div className="w-9 h-9 rounded-xl bg-primary-600 text-white flex items-center justify-center">
                <Phone size={18} />
              </div>
              <span className="text-lg font-semibold text-slate-900">LeadCRM</span>
              <span className="text-xs font-medium text-slate-400 bg-slate-100 px-2 py-0.5 rounded-full">Админ</span>
            </div>
            <div className="flex items-center gap-4">
              <span className="text-sm text-slate-600">{user?.full_name}</span>
              <button onClick={logout} className="p-2 text-slate-400 hover:text-slate-600 transition-default rounded-lg hover:bg-slate-100">
                <LogOut size={18} />
              </button>
            </div>
          </div>
        </div>
      </header>

      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6">
        <div className="flex gap-1 mb-6 bg-white rounded-xl p-1 border border-slate-200 w-fit">
          {[
            { key: 'overview', label: 'Обзор', icon: BarChart3 },
            { key: 'sellers', label: 'Продавцы', icon: Users },
            { key: 'leads', label: 'Лиды', icon: Phone },
            { key: 'parser', label: 'Парсер', icon: Terminal },
            { key: 'portfolio', label: 'Портфолио', icon: FolderOpen },
          ].map((t) => (
            <button
              key={t.key}
              onClick={() => setTab(t.key as Tab)}
              className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-default ${
                tab === t.key ? 'bg-primary-600 text-white' : 'text-slate-600 hover:bg-slate-100'
              }`}
            >
              <t.icon size={16} />
              {t.label}
            </button>
          ))}
        </div>

        {tab === 'overview' && stats && (
          <div className="space-y-6">
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <StatCard icon={Phone} label="Всего лидов" value={stats.total_leads} color="slate" />
              <StatCard icon={CheckCircle} label="Согласились" value={stats.confirmed_leads} color="emerald" />
              <StatCard icon={XCircle} label="Отказались" value={stats.declined_leads} color="red" />
              <StatCard icon={TrendingUp} label="Конверсия" value={`${stats.conversion_rate}%`} color="blue" />
            </div>

            <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
              <MiniStat label="Новые" value={stats.new_leads} />
              <MiniStat label="Назначены" value={stats.assigned_leads} />
              <MiniStat label="В работе" value={stats.calling_leads} />
              <MiniStat label="Перезвонить" value={stats.followup_leads} />
              <MiniStat label="Не ответили" value={stats.no_answer_leads} />
            </div>

            <div className="bg-white rounded-2xl border border-slate-200 overflow-hidden">
              <div className="px-6 py-4 border-b border-slate-100">
                <h3 className="font-semibold text-slate-900">Статистика продавцов</h3>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full">
                  <thead>
                    <tr className="bg-slate-50">
                      <th className="text-left px-6 py-3 text-xs font-medium text-slate-500 uppercase">Продавец</th>
                      <th className="text-center px-4 py-3 text-xs font-medium text-slate-500 uppercase">Назначено</th>
                      <th className="text-center px-4 py-3 text-xs font-medium text-slate-500 uppercase">Согласились</th>
                      <th className="text-center px-4 py-3 text-xs font-medium text-slate-500 uppercase">Отказались</th>
                      <th className="text-center px-4 py-3 text-xs font-medium text-slate-500 uppercase">Конверсия</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {stats.seller_stats.map((s) => {
                      const processed = s.confirmed + s.declined;
                      const conv = processed > 0 ? ((s.confirmed / processed) * 100).toFixed(1) : '—';
                      return (
                        <tr key={s.id} className="hover:bg-slate-50">
                          <td className="px-6 py-3">
                            <div className="flex items-center gap-2">
                              <span className="font-medium text-slate-900">{s.name}</span>
                              {!s.is_active && <span className="text-xs text-slate-400">(выкл)</span>}
                            </div>
                          </td>
                          <td className="text-center px-4 py-3 text-slate-600">{s.assigned}</td>
                          <td className="text-center px-4 py-3 text-emerald-600 font-medium">{s.confirmed}</td>
                          <td className="text-center px-4 py-3 text-red-600 font-medium">{s.declined}</td>
                          <td className="text-center px-4 py-3 text-slate-600">{conv}%</td>
                        </tr>
                      );
                    })}
                    {stats.seller_stats.length === 0 && (
                      <tr>
                        <td colSpan={5} className="text-center py-8 text-slate-400">Нет продавцов</td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        )}

        {tab === 'sellers' && (
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <h2 className="text-lg font-semibold text-slate-900">Продавцы ({sellers.length})</h2>
              <button
                onClick={() => setShowCreateForm(!showCreateForm)}
                className="flex items-center gap-2 px-4 py-2 bg-primary-600 text-white rounded-xl text-sm font-medium hover:bg-primary-700 transition-default"
              >
                <UserPlus size={16} />
                Добавить
              </button>
            </div>

            {showCreateForm && (
              <div className="bg-white rounded-2xl border border-slate-200 p-6">
                <h3 className="font-semibold text-slate-900 mb-4">Новый продавец</h3>
                <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                  <input
                    placeholder="ФИО"
                    value={newSeller.full_name}
                    onChange={(e) => setNewSeller({ ...newSeller, full_name: e.target.value })}
                    className="px-4 py-2.5 rounded-xl border border-slate-200 focus:border-primary-500 focus:ring-2 focus:ring-primary-100 outline-none transition-default"
                  />
                  <input
                    placeholder="Логин"
                    value={newSeller.username}
                    onChange={(e) => setNewSeller({ ...newSeller, username: e.target.value })}
                    className="px-4 py-2.5 rounded-xl border border-slate-200 focus:border-primary-500 focus:ring-2 focus:ring-primary-100 outline-none transition-default"
                  />
                  <input
                    placeholder="Пароль"
                    type="password"
                    value={newSeller.password}
                    onChange={(e) => setNewSeller({ ...newSeller, password: e.target.value })}
                    className="px-4 py-2.5 rounded-xl border border-slate-200 focus:border-primary-500 focus:ring-2 focus:ring-primary-100 outline-none transition-default"
                  />
                </div>
                <div className="flex gap-2 mt-4">
                  <button
                    onClick={createSeller}
                    disabled={creating}
                    className="px-6 py-2.5 bg-primary-600 text-white rounded-xl text-sm font-medium hover:bg-primary-700 disabled:opacity-50 transition-default"
                  >
                    {creating ? 'Создание...' : 'Создать'}
                  </button>
                  <button
                    onClick={() => setShowCreateForm(false)}
                    className="px-6 py-2.5 bg-slate-100 text-slate-600 rounded-xl text-sm font-medium hover:bg-slate-200 transition-default"
                  >
                    Отмена
                  </button>
                </div>
              </div>
            )}

            <div className="space-y-3">
              {sellers.map((seller) => (
                <div key={seller.id} className="bg-white rounded-2xl border border-slate-200 p-5 flex items-center justify-between">
                  <div>
                    <div className="flex items-center gap-2">
                      <span className="font-medium text-slate-900">{seller.full_name}</span>
                      <span className={`text-xs px-2 py-0.5 rounded-full ${seller.is_active ? 'bg-emerald-100 text-emerald-700' : 'bg-slate-100 text-slate-500'}`}>
                        {seller.is_active ? 'Активен' : 'Выключен'}
                      </span>
                    </div>
                    <div className="text-sm text-slate-500 mt-1">@{seller.username}</div>
                  </div>
                  <div className="flex items-center gap-2">
                    <button
                      onClick={() => batchAssign(seller.id)}
                      className="px-3 py-1.5 text-xs font-medium bg-blue-50 text-blue-600 rounded-lg hover:bg-blue-100 transition-default"
                    >
                      Назначить лиды
                    </button>
                    <button
                      onClick={() => toggleSeller(seller)}
                      className={`px-3 py-1.5 text-xs font-medium rounded-lg transition-default ${
                        seller.is_active ? 'bg-orange-50 text-orange-600 hover:bg-orange-100' : 'bg-emerald-50 text-emerald-600 hover:bg-emerald-100'
                      }`}
                    >
                      {seller.is_active ? 'Выключить' : 'Включить'}
                    </button>
                    <button
                      onClick={() => resetPassword(seller)}
                      className="px-3 py-1.5 text-xs font-medium bg-slate-50 text-slate-600 rounded-lg hover:bg-slate-100 transition-default"
                    >
                      Сброс пароля
                    </button>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {tab === 'leads' && (
          <div className="space-y-4">
            <div className="flex items-center justify-between gap-4 flex-wrap">
              <h2 className="text-lg font-semibold text-slate-900">Лиды ({leads.length})</h2>
              <div className="flex items-center gap-3">
                <select
                  value={leadFilter}
                  onChange={(e) => setLeadFilter(e.target.value)}
                  className="px-3 py-2 rounded-xl border border-slate-200 text-sm outline-none focus:border-primary-500"
                >
                  <option value="">Все статусы</option>
                  {Object.entries(statusLabels).map(([k, v]) => (
                    <option key={k} value={k}>{v}</option>
                  ))}
                </select>
                <div className="relative">
                  <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
                  <input
                    placeholder="Поиск..."
                    value={leadSearch}
                    onChange={(e) => setLeadSearch(e.target.value)}
                    onKeyDown={(e) => e.key === 'Enter' && loadLeads()}
                    className="pl-9 pr-4 py-2 rounded-xl border border-slate-200 text-sm outline-none focus:border-primary-500 w-48"
                  />
                </div>
                <button onClick={loadLeads} className="p-2 rounded-xl border border-slate-200 hover:bg-slate-50 transition-default">
                  <RefreshCw size={16} className={leadsLoading ? 'animate-spin' : ''} />
                </button>
              </div>
            </div>

            <div className="bg-white rounded-2xl border border-slate-200 overflow-hidden">
              <div className="overflow-x-auto">
                <table className="w-full">
                  <thead>
                    <tr className="bg-slate-50 border-b border-slate-100">
                      <th className="text-left px-5 py-3 text-xs font-medium text-slate-500 uppercase">Название</th>
                      <th className="text-left px-4 py-3 text-xs font-medium text-slate-500 uppercase">Телефон</th>
                      <th className="text-left px-4 py-3 text-xs font-medium text-slate-500 uppercase">Адрес</th>
                      <th className="text-center px-4 py-3 text-xs font-medium text-slate-500 uppercase">Статус</th>
                      <th className="text-left px-4 py-3 text-xs font-medium text-slate-500 uppercase">Продавец</th>
                      <th className="text-center px-4 py-3 text-xs font-medium text-slate-500 uppercase">Действия</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {leads.map((lead) => (
                      <tr key={lead.id} className="hover:bg-slate-50 transition-default">
                        <td className="px-5 py-3">
                          <div className="font-medium text-slate-900 text-sm">{lead.name}</div>
                          <div className="text-xs text-slate-400 mt-0.5">{(lead.categories || []).join(', ')}</div>
                        </td>
                        <td className="px-4 py-3 text-sm text-slate-600">{lead.phone || '—'}</td>
                        <td className="px-4 py-3 text-sm text-slate-500 max-w-[200px] truncate">{lead.address || '—'}</td>
                        <td className="px-4 py-3 text-center">
                          <span className={`inline-block px-2.5 py-1 rounded-full text-xs font-medium ${statusColors[lead.status] || 'bg-slate-100'}`}>
                            {statusLabels[lead.status] || lead.status}
                          </span>
                        </td>
                        <td className="px-4 py-3 text-sm text-slate-600">{lead.assigned_seller_name || '—'}</td>
                        <td className="px-4 py-3 text-center">
                          <div className="flex items-center justify-center gap-1">
                            <button
                              onClick={() => setSelectedLead(lead)}
                              className="p-1.5 rounded-lg hover:bg-slate-100 text-slate-400 hover:text-slate-600 transition-default"
                              title="Подробнее"
                            >
                              <Eye size={14} />
                            </button>
                            {!lead.assigned_to ? (
                              <button
                                onClick={() => setAssignModal(lead.id)}
                                className="p-1.5 rounded-lg hover:bg-blue-50 text-slate-400 hover:text-blue-600 transition-default"
                                title="Назначить"
                              >
                                <UserPlus size={14} />
                              </button>
                            ) : (
                              <button
                                onClick={() => unassignLead(lead.id)}
                                className="p-1.5 rounded-lg hover:bg-red-50 text-slate-400 hover:text-red-600 transition-default"
                                title="Снять"
                              >
                                <UserX size={14} />
                              </button>
                            )}
                          </div>
                        </td>
                      </tr>
                    ))}
                    {leads.length === 0 && (
                      <tr>
                        <td colSpan={6} className="text-center py-12 text-slate-400">
                          {leadsLoading ? 'Загрузка...' : 'Нет лидов'}
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        )}

        {tab === 'parser' && (
          <div className="space-y-6">
            <div className="bg-white rounded-2xl border border-slate-200 p-6">
              <h2 className="text-lg font-semibold text-slate-900 mb-4">Запустить парсинг</h2>
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
                <div>
                  <label className="block text-sm font-medium text-slate-700 mb-2">Запрос</label>
                  <input
                    type="text"
                    value={parserQuery}
                    onChange={(e) => setParserQuery(e.target.value)}
                    placeholder="Введите запрос..."
                    className="w-full px-4 py-2.5 rounded-xl border border-slate-200 focus:border-primary-500 focus:ring-2 focus:ring-primary-100 outline-none transition-default"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-slate-700 mb-2">
                    <MapPin size={14} className="inline mr-1" />
                    Город
                  </label>
                  <select
                    value={parserLocation}
                    onChange={(e) => setParserLocation(e.target.value)}
                    className="w-full px-4 py-2.5 rounded-xl border border-slate-200 focus:border-primary-500 focus:ring-2 focus:ring-primary-100 outline-none transition-default"
                  >
                    <option value="Москва">Москва</option>
                    <option value="Волгоград">Волгоград</option>
                    <option value="Казань">Казань</option>
                    <option value="Астрахань">Астрахань</option>
                    <option value="Санкт-Петербург">Санкт-Петербург</option>
                    <option value="Кисловодск">Кисловодск</option>
                  </select>
                </div>
                <div>
                  <label className="block text-sm font-medium text-slate-700 mb-2">Лимит</label>
                  <input
                    type="number"
                    value={parserLimit}
                    onChange={(e) => setParserLimit(parseInt(e.target.value) || 20)}
                    min="1"
                    max="1000"
                    className="w-full px-4 py-2.5 rounded-xl border border-slate-200 focus:border-primary-500 focus:ring-2 focus:ring-primary-100 outline-none transition-default"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-slate-700 mb-2">Режим</label>
                  <select
                    value={parserMode}
                    onChange={(e) => setParserMode(e.target.value)}
                    className="w-full px-4 py-2.5 rounded-xl border border-slate-200 focus:border-primary-500 focus:ring-2 focus:ring-primary-100 outline-none transition-default"
                  >
                    <option value="scrape">scrape</option>
                    <option value="run">run</option>
                  </select>
                </div>
              </div>
              <button
                onClick={runParser}
                disabled={parserLoading || !parserQuery.trim()}
                className="mt-4 flex items-center gap-2 px-6 py-2.5 bg-primary-600 text-white rounded-xl text-sm font-medium hover:bg-primary-700 disabled:opacity-50 transition-default"
              >
                <Play size={16} />
                {parserLoading ? 'Запуск...' : 'Запустить парсинг'}
              </button>
            </div>

            <div className="bg-white rounded-2xl border border-slate-200 overflow-hidden">
              <div className="px-6 py-4 border-b border-slate-100 flex items-center justify-between">
                <h3 className="font-semibold text-slate-900">Задачи парсера ({parserTasks.length})</h3>
                <button
                  onClick={loadParserTasks}
                  className="p-2 rounded-lg hover:bg-slate-100 transition-default"
                >
                  <RefreshCw size={16} className={parserRunning ? 'animate-spin' : ''} />
                </button>
              </div>
              <div className="divide-y divide-slate-100">
                {parserTasks.map((task) => (
                  <div key={task.task_id} className="p-5 hover:bg-slate-50 transition-default">
                    <div className="flex items-start justify-between gap-4">
                      <div className="flex-1">
                        <div className="flex items-center gap-3 mb-2">
                          <span className={`inline-block px-2.5 py-1 rounded-full text-xs font-medium ${
                            task.status === 'running' ? 'bg-blue-100 text-blue-700' :
                            task.status === 'completed' ? 'bg-emerald-100 text-emerald-700' :
                            task.status === 'failed' ? 'bg-red-100 text-red-700' :
                            'bg-slate-100 text-slate-700'
                          }`}>
                            {task.status}
                          </span>
                          <span className="text-sm font-medium text-slate-900">{task.query}</span>
                        </div>
                        <div className="flex items-center gap-4 text-sm text-slate-500">
                          <span className="flex items-center gap-1">
                            <MapPin size={14} />
                            {task.location}
                          </span>
                          <span>Лимит: {task.limit}</span>
                          <span>Режим: {task.mode}</span>
                        </div>
                        {task.output && (
                          <div className="mt-3 p-3 bg-slate-50 rounded-lg text-sm text-slate-600 max-h-32 overflow-y-auto">
                            <pre className="whitespace-pre-wrap font-mono text-xs">{task.output}</pre>
                          </div>
                        )}
                      </div>
                      {(task.status === 'running' || task.status === 'pending') && (
                        <button
                          onClick={() => stopParserTask(task.task_id)}
                          className="flex items-center gap-1 px-3 py-1.5 text-xs font-medium bg-red-50 text-red-600 rounded-lg hover:bg-red-100 transition-default"
                        >
                          <Square size={14} />
                          Стоп
                        </button>
                      )}
                    </div>
                  </div>
                ))}
                {parserTasks.length === 0 && (
                  <div className="text-center py-12 text-slate-400">
                    Нет задач
                  </div>
                )}
              </div>
            </div>
          </div>
        )}

        {tab === 'portfolio' && <PortfolioAdmin />}
      </div>

      {assignModal !== null && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={() => setAssignModal(null)}>
          <div className="bg-white rounded-2xl p-6 w-80 shadow-xl" onClick={(e) => e.stopPropagation()}>
            <h3 className="font-semibold text-slate-900 mb-4">Назначить продавцу</h3>
            <div className="space-y-2">
              {sellers.filter(s => s.is_active).map((seller) => (
                <button
                  key={seller.id}
                  onClick={() => { assignLead(assignModal, seller.id); }}
                  className="w-full text-left px-4 py-3 rounded-xl hover:bg-slate-50 text-slate-700 transition-default"
                >
                  {seller.full_name}
                </button>
              ))}
            </div>
          </div>
        </div>
      )}

      {selectedLead && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={() => setSelectedLead(null)}>
          <div className="bg-white rounded-2xl p-6 w-full max-w-lg shadow-xl max-h-[80vh] overflow-y-auto" onClick={(e) => e.stopPropagation()}>
            <h3 className="font-semibold text-slate-900 text-lg mb-4">{selectedLead.name}</h3>
            <div className="space-y-3 text-sm">
              <InfoRow label="Телефон" value={selectedLead.phone} />
              <InfoRow label="Email" value={selectedLead.email} />
              <InfoRow label="Адрес" value={selectedLead.address} />
              <InfoRow label="Категории" value={(selectedLead.categories || []).join(', ')} />
              <InfoRow label="Рейтинг" value={selectedLead.rating} />
              <InfoRow label="Отзывы" value={selectedLead.reviews} />
              <InfoRow label="Часы работы" value={selectedLead.hours} />
              <InfoRow label="Сайт" value={selectedLead.website || 'нет'} />
              <InfoRow label="Статус сайта" value={selectedLead.website_status} />
              <InfoRow label="Статус лида" value={statusLabels[selectedLead.status] || selectedLead.status} />
              <InfoRow label="Продавец" value={selectedLead.assigned_seller_name || 'не назначен'} />
              {selectedLead.notes && <InfoRow label="Заметки" value={selectedLead.notes} />}
            </div>
            <button
              onClick={() => setSelectedLead(null)}
              className="mt-6 w-full py-2.5 bg-slate-100 text-slate-600 rounded-xl text-sm font-medium hover:bg-slate-200 transition-default"
            >
              Закрыть
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function StatCard({ icon: Icon, label, value, color }: { icon: any; label: string; value: number | string; color: string }) {
  const colors: Record<string, string> = {
    slate: 'bg-slate-50 text-slate-600',
    emerald: 'bg-emerald-50 text-emerald-600',
    red: 'bg-red-50 text-red-600',
    blue: 'bg-blue-50 text-blue-600',
  };
  return (
    <div className="bg-white rounded-2xl border border-slate-200 p-5">
      <div className={`w-10 h-10 rounded-xl flex items-center justify-center mb-3 ${colors[color]}`}>
        <Icon size={20} />
      </div>
      <div className="text-2xl font-bold text-slate-900">{value}</div>
      <div className="text-sm text-slate-500 mt-0.5">{label}</div>
    </div>
  );
}

function MiniStat({ label, value }: { label: string; value: number }) {
  return (
    <div className="bg-white rounded-xl border border-slate-200 px-4 py-3 text-center">
      <div className="text-lg font-semibold text-slate-900">{value}</div>
      <div className="text-xs text-slate-500">{label}</div>
    </div>
  );
}

function InfoRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-3">
      <span className="text-slate-400 w-28 shrink-0">{label}</span>
      <span className="text-slate-700">{value || '—'}</span>
    </div>
  );
}
