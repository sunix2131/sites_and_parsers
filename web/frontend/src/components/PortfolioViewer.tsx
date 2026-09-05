import { useState, useEffect, useCallback } from 'react';
import { api } from '../api/client';
import {
  FolderOpen, ChevronRight, ArrowLeft, Image, Link, X, Eye, Copy
} from 'lucide-react';

interface Category {
  id: number;
  name: string;
  slug: string;
  description: string;
  projects_count: number;
}

interface Screenshot {
  id: number;
  filename: string;
  original_filename: string;
  url: string;
}

interface Project {
  id: number;
  name: string;
  slug: string;
  description: string;
  client_name: string;
  screenshots: Screenshot[];
}

type View = 'categories' | 'projects' | 'screenshots';

interface Props {
  onClose: () => void;
}

export default function PortfolioViewer({ onClose }: Props) {
  const [categories, setCategories] = useState<Category[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedCategory, setSelectedCategory] = useState<Category | null>(null);
  const [selectedProject, setSelectedProject] = useState<Project | null>(null);
  const [view, setView] = useState<View>('categories');
  const [loading, setLoading] = useState(false);
  const [previewImage, setPreviewImage] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const loadCategories = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.get<Category[]>('/portfolio/categories?active_only=true');
      setCategories(data);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadProjects = useCallback(async (categorySlug: string) => {
    setLoading(true);
    try {
      const data = await api.get<Project[]>(`/portfolio/categories/${categorySlug}/projects`);
      setProjects(data);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadCategories();
  }, [loadCategories]);

  const openProjects = (category: Category) => {
    setSelectedCategory(category);
    setView('projects');
    loadProjects(category.slug);
  };

  const openScreenshots = (project: Project) => {
    setSelectedProject(project);
    setView('screenshots');
  };

  const copyLink = (type: 'category' | 'project') => {
    let url = '';
    if (type === 'category' && selectedCategory) {
      url = `${window.location.origin}/portfolio/${selectedCategory.slug}`;
    } else if (type === 'project' && selectedProject && selectedCategory) {
      url = `${window.location.origin}/portfolio/${selectedCategory.slug}/${selectedProject.slug}`;
    }
    navigator.clipboard.writeText(url);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const goBack = () => {
    if (view === 'screenshots') {
      setView('projects');
      setSelectedProject(null);
    } else if (view === 'projects') {
      setView('categories');
      setSelectedCategory(null);
      setProjects([]);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-3xl w-full max-w-4xl h-[85vh] flex flex-col shadow-2xl overflow-hidden">
        <div className="px-6 py-4 border-b border-slate-200 flex items-center justify-between shrink-0">
          <div className="flex items-center gap-3">
            <button onClick={goBack} className="p-2 rounded-lg hover:bg-slate-100 transition-default">
              <ArrowLeft size={18} />
            </button>
            <div className="flex items-center gap-2 text-sm">
              <span
                className={`cursor-pointer hover:text-primary-600 ${view === 'categories' ? 'text-primary-600 font-medium' : 'text-slate-500'}`}
                onClick={() => { setView('categories'); setSelectedCategory(null); setSelectedProject(null); setProjects([]); }}
              >
                Портфолио
              </span>
              {selectedCategory && (
                <>
                  <ChevronRight size={14} className="text-slate-400" />
                  <span
                    className={`cursor-pointer hover:text-primary-600 ${view === 'projects' ? 'text-primary-600 font-medium' : 'text-slate-500'}`}
                    onClick={() => { setView('projects'); setSelectedProject(null); }}
                  >
                    {selectedCategory.name}
                  </span>
                </>
              )}
              {selectedProject && (
                <>
                  <ChevronRight size={14} className="text-slate-400" />
                  <span className="text-primary-600 font-medium">{selectedProject.name}</span>
                </>
              )}
            </div>
          </div>
          <div className="flex items-center gap-2">
            {(view === 'projects' || view === 'screenshots') && (
              <button
                onClick={() => copyLink(view === 'screenshots' ? 'project' : 'category')}
                className="flex items-center gap-2 px-3 py-1.5 text-sm font-medium bg-primary-50 text-primary-600 rounded-lg hover:bg-primary-100 transition-default"
              >
                {copied ? <CheckIcon /> : <Link size={14} />}
                {copied ? 'Скопировано!' : 'Скопировать ссылку'}
              </button>
            )}
            <button onClick={onClose} className="p-2 rounded-lg hover:bg-slate-100 text-slate-400 hover:text-slate-600 transition-default">
              <X size={20} />
            </button>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto p-6">
          {view === 'categories' && (
            <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
              {categories.map((cat) => (
                <button
                  key={cat.id}
                  onClick={() => openProjects(cat)}
                  className="bg-slate-50 rounded-2xl p-6 text-left hover:bg-primary-50 hover:border-primary-200 border border-slate-200 transition-default group"
                >
                  <div className="w-14 h-14 rounded-xl bg-white shadow-sm flex items-center justify-center mb-4 group-hover:bg-primary-100 transition-default">
                    <FolderOpen size={24} className="text-primary-600" />
                  </div>
                  <h3 className="font-semibold text-slate-900 mb-1">{cat.name}</h3>
                  <p className="text-sm text-slate-500">{cat.projects_count} проектов</p>
                </button>
              ))}
              {!loading && categories.length === 0 && (
                <div className="col-span-full text-center py-16 text-slate-400">
                  <FolderOpen size={48} className="mx-auto mb-4 opacity-30" />
                  <p className="text-lg font-medium">Портфолио пока пустое</p>
                </div>
              )}
            </div>
          )}

          {view === 'projects' && (
            <div className="space-y-3">
              {projects.map((proj) => (
                <button
                  key={proj.id}
                  onClick={() => openScreenshots(proj)}
                  className="w-full bg-white rounded-2xl border border-slate-200 p-5 flex items-center gap-4 hover:shadow-md hover:border-primary-200 transition-default text-left"
                >
                  <div className="w-16 h-16 rounded-xl bg-slate-100 flex items-center justify-center shrink-0 overflow-hidden">
                    {proj.screenshots.length > 0 ? (
                      <img
                        src={proj.screenshots[0].url}
                        alt=""
                        className="w-full h-full object-cover"
                      />
                    ) : (
                      <Image size={24} className="text-slate-400" />
                    )}
                  </div>
                  <div className="flex-1 min-w-0">
                    <h3 className="font-semibold text-slate-900 truncate">{proj.name}</h3>
                    {proj.client_name && <p className="text-sm text-slate-500">Клиент: {proj.client_name}</p>}
                    <p className="text-sm text-slate-400">{proj.screenshots.length} скриншотов</p>
                  </div>
                  <ChevronRight size={20} className="text-slate-400 shrink-0" />
                </button>
              ))}
              {!loading && projects.length === 0 && (
                <div className="text-center py-16 text-slate-400">
                  <Image size={48} className="mx-auto mb-4 opacity-30" />
                  <p className="text-lg font-medium">Нет проектов</p>
                </div>
              )}
            </div>
          )}

          {view === 'screenshots' && selectedProject && (
            <>
              {selectedProject.description && (
                <p className="text-slate-600 mb-4">{selectedProject.description}</p>
              )}
              <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
                {selectedProject.screenshots.map((shot) => (
                  <div
                    key={shot.id}
                    className="aspect-video bg-slate-100 rounded-xl overflow-hidden cursor-pointer group relative"
                    onClick={() => setPreviewImage(shot.url)}
                  >
                    <img
                      src={shot.url}
                      alt={shot.original_filename}
                      className="w-full h-full object-cover"
                    />
                    <div className="absolute inset-0 bg-black/0 group-hover:bg-black/30 transition-default flex items-center justify-center opacity-0 group-hover:opacity-100">
                      <Eye size={24} className="text-white" />
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      </div>

      {previewImage && (
        <div className="fixed inset-0 bg-black/90 flex items-center justify-center z-[60] p-4" onClick={() => setPreviewImage(null)}>
          <button className="absolute top-4 right-4 p-2 rounded-full bg-white/10 text-white hover:bg-white/20 transition-default" onClick={() => setPreviewImage(null)}>
            <X size={24} />
          </button>
          <img src={previewImage} alt="" className="max-w-full max-h-full rounded-lg" onClick={(e) => e.stopPropagation()} />
        </div>
      )}
    </div>
  );
}

function CheckIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="20 6 9 17 4 12"></polyline>
    </svg>
  );
}
