#pragma once
#include <Processors/IProcessor.h>
#include <Processors/Port.h>
#include <memory>

namespace DB
{

class ThreadGroup;
using ThreadGroupPtr = std::shared_ptr<ThreadGroup>;

/// Has one input and one output.
/// Works similarly to ISimpleTransform, but with much care about exceptions.
///
/// If input contain exception, this exception is pushed directly to output port.
/// If input contain data chunk, transform() is called for it.
/// When transform throws exception itself, data chunk is replaced by caught exception.
/// Transformed chunk or newly caught exception is pushed to output.
///
/// There may be any number of exceptions read from input, transform keeps the order.
/// It is expected that output port won't be closed from the other side before all data is processed.
///
/// Method onStart() is called before reading any data.
/// Method onFinish() is called after all data from input is processed, if no exception happened.
/// In case of exception, it is additionally pushed into pipeline.
class ExceptionKeepingTransform : public IProcessor
{
protected:
    InputPort & input;
    OutputPort & output;
    Port::Data data;

    enum class Stage : uint8_t
    {
        Start,
        Consume,
        Generate,
        Finish,
        Exception,
    };

    Stage stage = Stage::Start;
    bool ready_input = false;
    bool ready_output = false;
    const bool ignore_on_start_and_finish = true;

    struct GenerateResult
    {
        Chunk chunk;
        bool is_done = true;
    };

    virtual void onStart() {}
    virtual void onConsume(Chunk chunk) = 0;
    virtual GenerateResult onGenerate() = 0;
    virtual void onFinish() {}
    virtual void onException(std::exception_ptr /* exception */) { }

public:
    ExceptionKeepingTransform(SharedHeader in_header, SharedHeader out_header, bool ignore_on_start_and_finish_ = true);

    Status prepare() override;
    void work() override;

    InputPort & getInputPort() { return input; }
    OutputPort & getOutputPort() { return output; }

    void setRuntimeData(ThreadGroupPtr thread_group_);

private:
    ThreadGroupPtr thread_group = nullptr;
};

}
