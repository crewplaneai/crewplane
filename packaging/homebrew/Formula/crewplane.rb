class Crewplane < Formula
  include Language::Python::Virtualenv

  desc "Markdown-native control plane for AI coding CLIs"
  homepage "https://github.com/crewplaneai/crewplane"
  url "https://files.pythonhosted.org/packages/source/c/crewplane/crewplane-0.3.8.tar.gz"
  version "0.3.8"
  sha256 "60a0f20d2799cae5398df96d25f98be7878c89ceab3780f35b9d131ad25aba2a"
  license "Apache-2.0"
  head "https://github.com/crewplaneai/crewplane.git", branch: "master"

  depends_on "maturin" => :build
  depends_on "rust" => :build
  depends_on "libyaml"
  depends_on "python@3.13"

  resource "hatchling" do
    url "https://files.pythonhosted.org/packages/a9/84/1798b6d85ecde0e31546004efd25c5de1b1f49250644a60cce460e12593a/hatchling-1.32.0-py3-none-any.whl"
    sha256 "0e17c9c3b9aa7c625acc8d0f5b622f107d5049af9ecf5ada4de1aada5be7cdbc"
  end

  resource "packaging" do
    url "https://files.pythonhosted.org/packages/63/34/ba1c580383c9eada3711951fef0795c80b829a078d72188184bcab9dd527/packaging-26.3-py3-none-any.whl"
    sha256 "d7193f7c8e4e93f444fde0262bf90af30e16fa0ad0ad44cb553c87339b23cd1c"
  end

  resource "pathspec" do
    url "https://files.pythonhosted.org/packages/f1/d9/7fb5aa316bc299258e68c73ba3bddbc499654a07f151cba08f6153988714/pathspec-1.1.1-py3-none-any.whl"
    sha256 "a00ce642f577bf7f473932318056212bc4f8bfdf53128c78bbd5af0b9b20b189"
  end

  resource "pluggy" do
    url "https://files.pythonhosted.org/packages/54/20/4d324d65cc6d9205fabedc306948156824eb9f0ee1633355a8f7ec5c66bf/pluggy-1.6.0-py3-none-any.whl"
    sha256 "e920276dd6813095e9377c0bc5566d94c932c33b27a3e3945d8389c374dd4746"
  end

  resource "trove-classifiers" do
    url "https://files.pythonhosted.org/packages/7c/a4/81502f486f01db95bc8320646a8a12511f5e556cb63d5e224d91816605c4/trove_classifiers-2026.6.1.19-py3-none-any.whl"
    sha256 "ab4c4ec93cc4a4e7815fa759906e05e6bb3f2fbd92ea0f897288c6a43efd15b3"
  end

  resource "annotated-doc" do
    url "https://files.pythonhosted.org/packages/57/ba/046ceea27344560984e26a590f90bc7f4a75b06701f653222458922b558c/annotated_doc-0.0.4.tar.gz"
    sha256 "fbcda96e87e9c92ad167c2e53839e57503ecfda18804ea28102353485033faa4"
  end

  resource "annotated-types" do
    url "https://files.pythonhosted.org/packages/ee/67/531ea369ba64dcff5ec9c3402f9f51bf748cec26dde048a2f973a4eea7f5/annotated_types-0.7.0.tar.gz"
    sha256 "aff07c09a53a08bc8cfccb9c85b05f1aa9a2a6f23728d790723543408344ce89"
  end

  resource "click" do
    url "https://files.pythonhosted.org/packages/c7/0e/7fa0ef50764b67090eca4114772a2abf8b6148198475e54c660b97caeee6/click-8.5.0.tar.gz"
    sha256 "ba0d2089de75ea0310e2dde03160e6ca10009947fb95a182f9b54021bb272e34"
  end

  resource "markdown-it-py" do
    url "https://files.pythonhosted.org/packages/06/ff/7841249c247aa650a76b9ee4bbaeae59370dc8bfd2f6c01f3630c35eb134/markdown_it_py-4.2.0.tar.gz"
    sha256 "04a21681d6fbb623de53f6f364d352309d4094dd4194040a10fd51833e418d49"
  end

  resource "mdurl" do
    url "https://files.pythonhosted.org/packages/d6/54/cfe61301667036ec958cb99bd3efefba235e65cdeb9c84d24a8293ba1d90/mdurl-0.1.2.tar.gz"
    sha256 "bb413d29f5eea38f31dd4754dd7377d4465116fb207585f97bf925588687c1ba"
  end

  resource "pydantic" do
    url "https://files.pythonhosted.org/packages/6b/fb/6e44b63b26efea1cec48c26d8362313310202ef5ed6e7a52f1669e64e2cd/pydantic-2.14.0.tar.gz"
    sha256 "8a51a7aaddd60f55566d1f07bdd87b92b463903f39a8f26b71a06314cd1548ae"
  end

  resource "pydantic-core" do
    url "https://files.pythonhosted.org/packages/e6/6d/196e8c819e0e934f35a1a33b3530396feadb0af4ca38fe9f995249e55794/pydantic_core-2.50.0.tar.gz"
    sha256 "84d2d38f7d163c4dec292f379e9de1960c661795442aca6c90d706436cb3749e"
  end

  resource "pygments" do
    url "https://files.pythonhosted.org/packages/c3/b2/bc9c9196916376152d655522fdcebac55e66de6603a76a02bca1b6414f6c/pygments-2.20.0.tar.gz"
    sha256 "6757cd03768053ff99f3039c1a36d6c0aa0b263438fcab17520b30a303a82b5f"
  end

  resource "pyyaml" do
    url "https://files.pythonhosted.org/packages/05/8e/961c0007c59b8dd7729d542c61a4d537767a59645b82a0b521206e1e25c2/pyyaml-6.0.3.tar.gz"
    sha256 "d76623373421df22fb4cf8817020cbb7ef15c725b9d5e45f17e189bfc384190f"
  end

  resource "rich" do
    url "https://files.pythonhosted.org/packages/c0/8f/0722ca900cc807c13a6a0c696dacf35430f72e0ec571c4275d2371fca3e9/rich-15.0.0.tar.gz"
    sha256 "edd07a4824c6b40189fb7ac9bc4c52536e9780fbbfbddf6f1e2502c31b068c36"
  end

  resource "shellingham" do
    url "https://files.pythonhosted.org/packages/58/15/8b3609fd3830ef7b27b655beb4b4e9c62313a4e8da8c676e142cc210d58e/shellingham-1.5.4.tar.gz"
    sha256 "8dbca0739d487e5bd35ab3ca4b36e11c4078f3a234bfce294b0a0291363404de"
  end

  resource "typer" do
    url "https://files.pythonhosted.org/packages/03/51/d33db42cc72ffd8c30777547b42d01f0cbf9d95a770457698d0174b3ed71/typer-0.27.3.tar.gz"
    sha256 "d0396f770a560ab1b0a8504e13b5f254b728cedb05c61cf0359e944e50ce8901"
  end

  resource "typing-extensions" do
    url "https://files.pythonhosted.org/packages/f6/cc/6253133b5bb138fc3306cebfbda2c520f545d36b5be2c7255cc528bb45d6/typing_extensions-4.16.0.tar.gz"
    sha256 "dc983d19a509c94dba722ee6abd33940f7c05a89e243c47e907eb4db6f1a43e5"
  end

  resource "typing-inspection" do
    url "https://files.pythonhosted.org/packages/a3/26/b09b8010994eccc3c09092e6b34058f36a460eea2d4c3e8b910c695975a0/typing_inspection-0.4.4.tar.gz"
    sha256 "547274fa6b0a561ccf549cc9524b999a578e737d015d8709d021f9d0d13bea47"
  end

  def python3
    "python3.13"
  end

  def install
    build_resources = %w[
      hatchling
      packaging
      pathspec
      pluggy
      trove-classifiers
    ]

    venv = virtualenv_create(libexec, python3)
    venv.pip_install build_resources.map { |resource_name| resource(resource_name) }
    venv.pip_install resources.reject { |resource| build_resources.include?(resource.name) }
    venv.pip_install_and_link buildpath, build_isolation: false
  end

  test do
    assert_match "Usage", shell_output("#{bin}/crewplane --help")
  end
end
