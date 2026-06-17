import { useState, useEffect, useCallback } from 'react';
import { useAuth } from '../context/AuthContext';
import { api } from '../api/client';
import PortfolioViewer from '../components/PortfolioViewer';
import {
  Phone, LogOut, CheckCircle, XCircle, Clock, Search,
  RefreshCw, PhoneCall, PhoneOff, AlertTriangle, Timer,
  MapPin, Star, Globe, ExternalLink, MessageSquare, ChevronDown,
  FolderOpen, Hourglass
} from 'lucide-react';

interface Lead {
  id: number;
  name: string;
  categories: string[];
  address: string;
  phone: string;
  email: string;
  website: string;
  website_status: string;
  social_links: string[];
  rating: string;
  reviews: string;
  hours: string;
  yandex_url: string;
  status: string;
  notes: string;
  updated_at: string;
}

interface RateLimitInfo {
  actions_remaining: number;
  actions_used: number;
  limit: number;
  resets_at: string;
}

interface MyStats {
  total: number;
  assigned: number;
  calling: number;
  confirmed: number;
  declined: number;
  followup: number;
  no_answer: number;
  rate_limit: RateLimitInfo;
}

export default function SellerDashboard() {
  const { user, logout } = useAuth();
  const [leads, setLeads] = useState<Lead[]>([]);
  const [stats, setStats] = useState<MyStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState('assigned');
  const [search, setSearch] = useState('');
  const [selectedLead, setSelectedLead] = useState<Lead | null>(null);
  const [note, setNote] = useState('');
  const [updating, setUpdating] = useState(false);
  const [rateLimit, setRateLimit] = useState<RateLimitInfo | null>(null);
  const [showCallTimer, setShowCallTimer] = useState(false);
  const [callStartTime, setCallStartTime] = useState<number | null>(null);
  const [showPortfolio, setShowPortfolio] = useState(false);

  const loadLeads = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      if (filter) params.set('status', filter);
      if (search) params.set('search', search);
      params.set('per_page', '100');
      const data = await api.get<Lead[]>(`/leads?${params}`);
      setLeads(data);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  }, [filter, search]);

  const loadStats = useCallback(async () => {
    try {
      const data = await api.get<MyStats>('/me/stats');
      setStats(data);
      setRateLimit(data.rate_limit);
    } catch (err) {
      console.error(err);
    }
  }, []);

  useEffect(() => {
    loadLeads();
    loadStats();
  }, [loadLeads]);

  useEffect(() => {
    const interval = setInterval(loadStats, 30000);
    return () => clearInterval(interval);
  }, [loadStats]);

  const startCall = (lead: Lead) => {
    setSelectedLead(lead);
    setCallStartTime(Date.now());
    setShowCallTimer(true);
    api.post(`/leads/${lead.id}/status`, { status: 'calling', note: '' }).catch(console.error);
  };

  const endCall = async (status: string) => {
    if (!selectedLead) return;

    if (rateLimit && rateLimit.actions_remaining <= 0) {
      alert('Вы достигли лимита обновлений за час. Подождите немного.');
      return;
    }

    setUpdating(true);
    try {
      await api.post(`/leads/${selectedLead.id}/status`, { status, note });
      setNote('');
      setShowCallTimer(false);
      setSelectedLead(null);
      setCallStartTime(null);
      await loadLeads();
      await loadStats();
    } catch (err: any) {
      alert(err.message);
    } finally {
      setUpdating(false);
    }
  };

  const statusColors: Record<string, string> = {
    new: 'bg-slate-100 text-slate-700',
    assigned: 'bg-blue-100 text-blue-700',
    calling: 'bg-amber-100 text-amber-700',
    confirmed: 'bg-emerald-100 text-emerald-700',
    declined: 'bg-red-100 text-red-700',
    followup: 'bg-purple-100 text-purple-700',
    no_answer: 'bg-orange-100 text-orange-700',
    waiting: 'bg-cyan-100 text-cyan-700',
  };

  const statusLabels: Record<string, string> = {
    new: 'Новый',
    assigned: 'Назначен',
    calling: 'Звоню...',
    confirmed: 'Согласен',
    declined: 'Отказ',
    followup: 'Перезвонить',
    no_answer: 'Не ответил',
    waiting: 'Ждёт',
  };

  return (
    <div className="min-h-screen bg-slate-50">
      <header className="bg-white border-b border-slate-200 sticky top-0 z-40">
        <div className="max-w-6xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex items-center justify-between h-16">
            <div className="flex items-center gap-3">
              <div className="w-9 h-9 rounded-xl bg-primary-600 text-white flex items-center justify-center">
                <Phone size={18} />
              </div>
              <span className="text-lg font-semibold text-slate-900">LeadCRM</span>
            </div>
            <div className="flex items-center gap-4">
              {rateLimit && (
                <div className="hidden sm:flex items-center gap-2 text-sm">
                  <Timer size={14} className="text-slate-400" />
                  <span className={`font-medium ${rateLimit.actions_remaining <= 5 ? 'text-amber-600' : 'text-slate-500'}`}>
                    {rateLimit.actions_remaining}/{rateLimit.limit} осталось
                  </span>
                </div>
              )}
              <span className="text-sm text-slate-600">{user?.full_name}</span>
              <button onClick={logout} className="p-2 text-slate-400 hover:text-slate-600 transition-default rounded-lg hover:bg-slate-100">
                <LogOut size={18} />
              </button>
            </div>
          </div>
        </div>
      </header>

      <div className="max-w-6xl mx-auto px-4 sm:px-6 lg:px-8 py-6">
        {stats && (
          <div className="grid grid-cols-3 md:grid-cols-6 gap-3 mb-6">
            <MiniStatCard label="Назначено" value={stats.assigned} color="blue" />
            <MiniStatCard label="Звоню" value={stats.calling} color="amber" />
            <MiniStatCard label="Согласен" value={stats.confirmed} color="emerald" />
            <MiniStatCard label="Отказ" value={stats.declined} color="red" />
            <MiniStatCard label="Перезвонить" value={stats.followup} color="purple" />
            <MiniStatCard label="Не ответил" value={stats.no_answer} color="orange" />
          </div>
        )}

        <div className="flex flex-col sm:flex-row gap-3 mb-4">
          <div className="flex gap-1 bg-white rounded-xl p-1 border border-slate-200 overflow-x-auto">
            {[
              { key: 'assigned', label: 'Назначены' },
              { key: 'calling', label: 'В работе' },
              { key: 'followup', label: 'Перезвонить' },
              { key: 'confirmed', label: 'Согласен' },
              { key: 'declined', label: 'Отказ' },
              { key: '', label: 'Все' },
            ].map((f) => (
              <button
                key={f.key}
                onClick={() => setFilter(f.key)}
                className={`px-3 py-1.5 rounded-lg text-sm font-medium whitespace-nowrap transition-default ${
                  filter === f.key ? 'bg-primary-600 text-white' : 'text-slate-600 hover:bg-slate-100'
                }`}
              >
                {f.label}
              </button>
            ))}
          </div>
          <div className="relative flex-1">
            <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
            <input
              placeholder="Поиск по названию, телефону, адресу..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && loadLeads()}
              className="w-full pl-9 pr-4 py-2 rounded-xl border border-slate-200 text-sm outline-none focus:border-primary-500 bg-white"
            />
          </div>
          <button onClick={loadLeads} className="p-2 rounded-xl border border-slate-200 hover:bg-slate-50 transition-default bg-white">
            <RefreshCw size={16} className={loading ? 'animate-spin' : ''} />
          </button>
        </div>

        <div className="space-y-3">
          {leads.map((lead) => (
            <div key={lead.id} className="bg-white rounded-2xl border border-slate-200 overflow-hidden hover:shadow-sm transition-default">
              <div className="p-4 sm:p-5">
                <div className="flex items-start justify-between gap-4">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 mb-1">
                      <h3 className="font-semibold text-slate-900 truncate">{lead.name}</h3>
                      <span className={`shrink-0 px-2 py-0.5 rounded-full text-xs font-medium ${statusColors[lead.status]}`}>
                        {statusLabels[lead.status]}
                      </span>
                    </div>
                    {lead.categories?.length > 0 && (
                      <div className="flex flex-wrap gap-1 mb-2">
                        {lead.categories.map((cat, i) => (
                          <span key={i} className="text-xs px-2 py-0.5 bg-slate-50 text-slate-500 rounded-full">{cat}</span>
                        ))}
                      </div>
                    )}
                    <div className="space-y-1 text-sm text-slate-600">
                      {lead.phone && (
                        <a href={`tel:${lead.phone}`} className="flex items-center gap-2 hover:text-primary-600 transition-default">
                          <Phone size={14} className="text-slate-400" />
                          {lead.phone}
                        </a>
                      )}
                      {lead.address && (
                        <div className="flex items-center gap-2">
                          <MapPin size={14} className="text-slate-400" />
                          <span className="truncate">{lead.address}</span>
                        </div>
                      )}
                      {lead.rating && (
                        <div className="flex items-center gap-2">
                          <Star size={14} className="text-amber-400" />
                          {lead.rating} {lead.reviews && `(${lead.reviews} отзывов)`}
                        </div>
                      )}
                    </div>
                  </div>

                  <div className="flex flex-col gap-2 shrink-0">
                    {(lead.status === 'assigned' || lead.status === 'followup' || lead.status === 'no_answer') && (
                      <button
                        onClick={() => startCall(lead)}
                        className="flex items-center gap-2 px-4 py-2.5 bg-primary-600 text-white rounded-xl text-sm font-medium hover:bg-primary-700 transition-default"
                      >
                        <PhoneCall size={16} />
                        Позвонить
                      </button>
                    )}
                    {lead.status === 'calling' && (
                      <button
                        onClick={() => { setSelectedLead(lead); setShowCallTimer(true); }}
                        className="flex items-center gap-2 px-4 py-2.5 bg-amber-500 text-white rounded-xl text-sm font-medium hover:bg-amber-600 transition-default animate-pulse"
                      >
                        <PhoneCall size={16} />
                        Завершить
                      </button>
                    )}
                  </div>
                </div>

                {lead.notes && (
                  <div className="mt-3 p-3 bg-slate-50 rounded-xl text-sm text-slate-600">
                    <MessageSquare size={12} className="inline mr-1.5 text-slate-400" />
                    {lead.notes.split('\n').slice(-1)[0]}
                  </div>
                )}
              </div>

              <div className="border-t border-slate-100 px-4 sm:px-5 py-2 flex gap-4 text-xs text-slate-400 overflow-x-auto">
                {lead.email && <span>{lead.email}</span>}
                {lead.hours && <span>{lead.hours}</span>}
                {lead.website && <span className="text-primary-500">{lead.website}</span>}
                {lead.yandex_url && (
                  <a href={lead.yandex_url} target="_blank" rel="noreferrer" className="flex items-center gap-1 hover:text-primary-600">
                    <ExternalLink size={10} /> Я.Карты
                  </a>
                )}
              </div>
            </div>
          ))}

          {!loading && leads.length === 0 && (
            <div className="text-center py-16 text-slate-400">
              <Phone size={48} className="mx-auto mb-4 opacity-30" />
              <p className="text-lg font-medium">Нет лидов</p>
              <p className="text-sm mt-1">Попросите администратора назначить вам лиды</p>
            </div>
          )}
        </div>
      </div>

      {showCallTimer && selectedLead && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="bg-white rounded-3xl w-full max-w-md p-6 shadow-2xl">
            <div className="text-center mb-6">
              <div className="w-20 h-20 rounded-full bg-primary-100 flex items-center justify-center mx-auto mb-4">
                <PhoneCall size={36} className="text-primary-600" />
              </div>
              <h3 className="text-xl font-bold text-slate-900 mb-1">{selectedLead.name}</h3>
              {selectedLead.phone && (
                <a href={`tel:${selectedLead.phone}`} className="text-2xl font-semibold text-primary-600">
                  {selectedLead.phone}
                </a>
              )}
              {callStartTime && <CallTimer startTime={callStartTime} />}
            </div>

            <div className="mb-4">
              <label className="block text-sm font-medium text-slate-700 mb-1.5">Заметка</label>
              <textarea
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="Что обсудили, результат разговора..."
                rows={3}
                className="w-full px-4 py-3 rounded-xl border border-slate-200 focus:border-primary-500 focus:ring-2 focus:ring-primary-100 outline-none transition-default resize-none text-sm"
              />
            </div>

            <div className="space-y-2">
              <button
                onClick={() => endCall('confirmed')}
                disabled={updating}
                className="w-full flex items-center justify-center gap-2 px-4 py-3.5 bg-emerald-600 text-white rounded-xl font-medium hover:bg-emerald-700 disabled:opacity-50 transition-default"
              >
                <CheckCircle size={18} />
                Клиент согласен
              </button>
              <button
                onClick={() => endCall('declined')}
                disabled={updating}
                className="w-full flex items-center justify-center gap-2 px-4 py-3.5 bg-red-600 text-white rounded-xl font-medium hover:bg-red-700 disabled:opacity-50 transition-default"
              >
                <XCircle size={18} />
                Клиент отказался
              </button>
              <div className="grid grid-cols-2 gap-2">
                <button
                  onClick={() => endCall('waiting')}
                  disabled={updating}
                  className="flex items-center justify-center gap-2 px-4 py-3 bg-cyan-50 text-cyan-700 rounded-xl text-sm font-medium hover:bg-cyan-100 disabled:opacity-50 transition-default"
                >
                  <Hourglass size={16} />
                  Клиент ждёт
                </button>
                <button
                  onClick={() => endCall('followup')}
                  disabled={updating}
                  className="flex items-center justify-center gap-2 px-4 py-3 bg-purple-50 text-purple-700 rounded-xl text-sm font-medium hover:bg-purple-100 disabled:opacity-50 transition-default"
                >
                  <Clock size={16} />
                  Перезвонить
                </button>
              </div>
              <button
                onClick={() => endCall('no_answer')}
                disabled={updating}
                className="w-full flex items-center justify-center gap-2 px-4 py-3 bg-orange-50 text-orange-700 rounded-xl text-sm font-medium hover:bg-orange-100 disabled:opacity-50 transition-default"
              >
                <PhoneOff size={16} />
                Не ответил
              </button>
              <button
                onClick={() => { setShowCallTimer(false); setSelectedLead(null); }}
                className="w-full py-2 text-sm text-slate-400 hover:text-slate-600 transition-default"
              >
                Отмена
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function CallTimer({ startTime }: { startTime: number }) {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    const interval = setInterval(() => {
      setElapsed(Math.floor((Date.now() - startTime) / 1000));
    }, 1000);
    return () => clearInterval(interval);
  }, [startTime]);

  const minutes = Math.floor(elapsed / 60);
  const seconds = elapsed % 60;

  return (
    <div className="text-3xl font-mono text-slate-300 mt-2">
      {String(minutes).padStart(2, '0')}:{String(seconds).padStart(2, '0')}
    </div>
  );
}

function MiniStatCard({ label, value, color }: { label: string; value: number; color: string }) {
  const colors: Record<string, string> = {
    blue: 'text-blue-600',
    amber: 'text-amber-600',
    emerald: 'text-emerald-600',
    red: 'text-red-600',
    purple: 'text-purple-600',
    orange: 'text-orange-600',
  };
  return (
    <div className="bg-white rounded-xl border border-slate-200 px-3 py-2.5 text-center">
      <div className={`text-xl font-bold ${colors[color]}`}>{value}</div>
      <div className="text-[11px] text-slate-500">{label}</div>
    </div>
  );
}
