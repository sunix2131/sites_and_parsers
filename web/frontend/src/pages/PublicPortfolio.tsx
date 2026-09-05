import { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { FolderOpen, ArrowLeft, Image, X, Eye, ChevronRight } from 'lucide-react';

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

export default function PublicPortfolio() {
  const { categorySlug, projectSlug } = useParams();
  const navigate = useNavigate();
  const [categories, setCategories] = useState<Category[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedCategory, setSelectedCategory] = useState<Category | null>(null);
  const [selectedProject, setSelectedProject] = useState<Project | null>(null);
  const [loading, setLoading] = useState(true);
  const [previewImage, setPreviewImage] = useState<string | null>(null);

  useEffect(() => {
    if (!categorySlug) {
      loadCategories();
    } else if (!projectSlug) {
      loadProjects(categorySlug);
    } else {
      loadProject(projectSlug);
    }
  }, [categorySlug, projectSlug]);

  const loadCategories = async () => {
    setLoading(true);
    try {
      const res = await fetch('/api/portfolio/categories?active_only=true');
      const data = await res.json();
      setCategories(data);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  const loadProjects = async (slug: string) => {
    setLoading(true);
    try {
      const catsRes = await fetch('/api/portfolio/categories?active_only=true');
      const cats = await catsRes.json();
      const cat = cats.find((c: Category) => c.slug === slug);
      setSelectedCategory(cat || null);

      const res = await fetch(`/api/portfolio/categories/${slug}/projects`);
      const data = await res.json();
      setProjects(data);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  const loadProject = async (slug: string) => {
    setLoading(true);
    try {
      const res = await fetch(`/api/portfolio/projects/${slug}`);
      const data = await res.json();
      setSelectedProject(data);

      const catsRes = await fetch('/api/portfolio/categories?active_only=true');
      const cats = await catsRes.json();
      const cat = cats.find((c: Category) => c.id === data.category_id);
      setSelectedCategory(cat || null);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  const getImageUrl = (shot: Screenshot) => {
    return shot.url;
  };

  return (
    <div className="min-h-screen bg-slate-50">
      <header className="bg-white border-b border-slate-200 sticky top-0 z-40">
        <div className="max-w-6xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex items-center justify-between h-16">
            <div className="flex items-center gap-3">
              <div className="w-9 h-9 rounded-xl bg-primary-600 text-white flex items-center justify-center">
                <Image size={18} />
              </div>
              <span className="text-lg font-semibold text-slate-900">Портфолио</span>
            </div>
            {categorySlug && (
              <button
                onClick={() => {
                  if (projectSlug && selectedCategory) {
                    navigate(`/portfolio/${selectedCategory.slug}`);
                  } else {
                    navigate('/portfolio');
                  }
                }}
                className="flex items-center gap-2 text-sm text-slate-600 hover:text-primary-600 transition-default"
              >
                <ArrowLeft size={16} />
                Назад
              </button>
            )}
          </div>
        </div>
      </header>

      <div className="max-w-6xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {loading ? (
          <div className="flex items-center justify-center py-20">
            <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary-600"></div>
          </div>
        ) : !categorySlug ? (
          <div>
            <h1 className="text-3xl font-bold text-slate-900 mb-2">Наши работы</h1>
            <p className="text-slate-600 mb-8">Выберите категорию для просмотра проектов</p>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-6">
              {categories.map((cat) => (
                <button
                  key={cat.id}
                  onClick={() => navigate(`/portfolio/${cat.slug}`)}
                  className="bg-white rounded-2xl p-8 text-left hover:shadow-lg hover:border-primary-200 border border-slate-200 transition-default group"
                >
                  <div className="w-16 h-16 rounded-xl bg-primary-50 flex items-center justify-center mb-4 group-hover:bg-primary-100 transition-default">
                    <FolderOpen size={28} className="text-primary-600" />
                  </div>
                  <h2 className="text-xl font-semibold text-slate-900 mb-2">{cat.name}</h2>
                  {cat.description && <p className="text-sm text-slate-500 mb-2">{cat.description}</p>}
                  <p className="text-sm text-primary-600 font-medium">{cat.projects_count} проектов</p>
                </button>
              ))}
            </div>
            {categories.length === 0 && (
              <div className="text-center py-20 text-slate-400">
                <FolderOpen size={64} className="mx-auto mb-4 opacity-30" />
                <p className="text-xl font-medium">Портфолио пока пустое</p>
              </div>
            )}
          </div>
        ) : !projectSlug ? (
          <div>
            <div className="flex items-center gap-2 text-sm text-slate-500 mb-4">
              <button onClick={() => navigate('/portfolio')} className="hover:text-primary-600">Портфолио</button>
              <ChevronRight size={14} />
              <span className="text-slate-900 font-medium">{selectedCategory?.name}</span>
            </div>
            <h1 className="text-3xl font-bold text-slate-900 mb-2">{selectedCategory?.name}</h1>
            {selectedCategory?.description && <p className="text-slate-600 mb-8">{selectedCategory.description}</p>}
            <div className="space-y-4">
              {projects.map((proj) => (
                <button
                  key={proj.id}
                  onClick={() => navigate(`/portfolio/${selectedCategory?.slug}/${proj.slug}`)}
                  className="w-full bg-white rounded-2xl border border-slate-200 p-6 flex items-center gap-6 hover:shadow-lg hover:border-primary-200 transition-default text-left"
                >
                  <div className="w-24 h-24 rounded-xl bg-slate-100 flex items-center justify-center shrink-0 overflow-hidden">
                    {proj.screenshots.length > 0 ? (
                      <img src={getImageUrl(proj.screenshots[0])} alt="" className="w-full h-full object-cover" />
                    ) : (
                      <Image size={32} className="text-slate-400" />
                    )}
                  </div>
                  <div className="flex-1 min-w-0">
                    <h2 className="text-xl font-semibold text-slate-900 mb-1">{proj.name}</h2>
                    {proj.client_name && <p className="text-sm text-slate-500 mb-1">Клиент: {proj.client_name}</p>}
                    {proj.description && <p className="text-sm text-slate-600 line-clamp-2">{proj.description}</p>}
                    <p className="text-sm text-primary-600 font-medium mt-2">{proj.screenshots.length} скриншотов</p>
                  </div>
                  <ChevronRight size={24} className="text-slate-400 shrink-0" />
                </button>
              ))}
            </div>
            {projects.length === 0 && (
              <div className="text-center py-20 text-slate-400">
                <Image size={64} className="mx-auto mb-4 opacity-30" />
                <p className="text-xl font-medium">Нет проектов в этой категории</p>
              </div>
            )}
          </div>
        ) : (
          <div>
            <div className="flex items-center gap-2 text-sm text-slate-500 mb-4">
              <button onClick={() => navigate('/portfolio')} className="hover:text-primary-600">Портфолио</button>
              <ChevronRight size={14} />
              <button onClick={() => navigate(`/portfolio/${selectedCategory?.slug}`)} className="hover:text-primary-600">
                {selectedCategory?.name}
              </button>
              <ChevronRight size={14} />
              <span className="text-slate-900 font-medium">{selectedProject?.name}</span>
            </div>
            <h1 className="text-3xl font-bold text-slate-900 mb-2">{selectedProject?.name}</h1>
            {selectedProject?.client_name && <p className="text-slate-500 mb-2">Клиент: {selectedProject.client_name}</p>}
            {selectedProject?.description && <p className="text-slate-600 mb-8">{selectedProject.description}</p>}
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
              {selectedProject?.screenshots.map((shot) => (
                <div
                  key={shot.id}
                  className="aspect-video bg-slate-100 rounded-xl overflow-hidden cursor-pointer group relative"
                  onClick={() => setPreviewImage(getImageUrl(shot))}
                >
                  <img src={getImageUrl(shot)} alt={shot.original_filename} className="w-full h-full object-cover" />
                  <div className="absolute inset-0 bg-black/0 group-hover:bg-black/30 transition-default flex items-center justify-center opacity-0 group-hover:opacity-100">
                    <Eye size={32} className="text-white" />
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      {previewImage && (
        <div className="fixed inset-0 bg-black/90 flex items-center justify-center z-50 p-4" onClick={() => setPreviewImage(null)}>
          <button className="absolute top-4 right-4 p-2 rounded-full bg-white/10 text-white hover:bg-white/20 transition-default" onClick={() => setPreviewImage(null)}>
            <X size={24} />
          </button>
          <img src={previewImage} alt="" className="max-w-full max-h-full rounded-lg" onClick={(e) => e.stopPropagation()} />
        </div>
      )}
    </div>
  );
}
