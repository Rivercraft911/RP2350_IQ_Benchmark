// Minimal stand-in for the GNU Radio runtime, enough to compile the gr-dtv DVB-S2 block
// implementations standalone and call their work functions directly (no scheduler, no tags).
#pragma once
#include <complex>
#include <memory>
#include <string>
#include <vector>

typedef std::complex<float> gr_complex;
typedef std::vector<int> gr_vector_int;
typedef std::vector<const void*> gr_vector_const_void_star;
typedef std::vector<void*> gr_vector_void_star;

namespace gr {
struct io_signature {
    typedef std::shared_ptr<io_signature> sptr;
    static sptr make(int, int, int) { return nullptr; }
};
struct logger {
    template <class... A> void warn(A&&...) { ++warnings; }
    int warnings = 0;
};
class block {
public:
    block() = default;
    block(const std::string&, io_signature::sptr, io_signature::sptr)
        : d_logger(std::make_shared<logger>()) {}
    virtual ~block() = default;
    void set_output_multiple(int) {}
    void consume_each(int n) { consumed = n; }
    virtual void forecast(int, gr_vector_int&) {}
    virtual int general_work(int, gr_vector_int&, gr_vector_const_void_star&, gr_vector_void_star&) { return 0; }
    std::shared_ptr<logger> d_logger;
    int consumed = 0;
};
class sync_block : public block {
public:
    sync_block() = default;
    sync_block(const std::string& n, io_signature::sptr i, io_signature::sptr o) : block(n, i, o) {}
    virtual int work(int, gr_vector_const_void_star&, gr_vector_void_star&) = 0;
};
} // namespace gr

namespace gnuradio {
template <class T, class... A> std::shared_ptr<T> make_block_sptr(A&&... a)
{
    return std::make_shared<T>(std::forward<A>(a)...);
}
} // namespace gnuradio
